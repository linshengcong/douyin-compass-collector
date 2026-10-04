"""Serial platform-neutral page persistence and category lifecycle orchestration."""

from datetime import datetime
from zoneinfo import ZoneInfo
from compass_collector.category_batch import PreparedCategoryBatch
from compass_collector.config import TaskConfig
from compass_collector.errors import (
    AuthRequiredError,
    BrowserOperationError,
    CategoryBatchCollectionError,
    CollectionInterruptedError,
    CollectorError,
    HttpRequestError,
    HttpResponseError,
    ResponseContractError,
)
from compass_collector.models import (
    CategoryRunPlan,
    CollectedCategoryBatch,
    CollectedCategoryRun,
    RawPageRecord,
)
from compass_collector.persistence import Database
from compass_collector.platforms.contracts import PlatformAdapter
from compass_collector.run_control import CollectionControl
from compass_collector.runtime_logging import LogContext, RuntimeLogger

# 共享流程只理解安全错误分类，平台错误码由适配器转换。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
ORDINARY_CATEGORY_ERRORS = (HttpRequestError, HttpResponseError, ResponseContractError)
MAX_CONSECUTIVE_PLATFORM_UNAVAILABLE_FAILURES = 3
PLATFORM_TEMPORARY_UNAVAILABLE_ERROR_CATEGORY = "platform_temporarily_unavailable"
# 淘宝未知业务错误使用自身分类，不能套用罗盘11001含义。
TAOBAO_BUSINESS_ERROR_CATEGORY = "taobao_business_error"
# 淘宝完整首轮后最多补采两轮，罗盘不进入分类补采。
TAOBAO_CATEGORY_RETRY_ROUNDS = 2


def _safe_emit(runtime_logger, **fields):
    """Keep diagnostic logging failures from changing business results."""
    try:
        runtime_logger.emit(**fields)
    except Exception:
        pass


def _sync_collection_snapshot(storage, snapshot):
    """Retry a transient Manifest write without repeating PostgreSQL transactions."""
    # 权威数据库快照只同步镜像，绝不重放已经提交的页。
    for attempt in range(2):
        try:
            storage.sync_collection_snapshot(snapshot)
            return
        except Exception:
            if attempt == 1:
                raise


def _raise_if_stopped(control):
    """Convert GUI cancellation into the shared terminal error."""
    if control is not None and control.stop_requested():
        raise CollectionInterruptedError(
            "Collection interrupted", category="interrupted"
        )


class _CategoryAttemptFailed(Exception):
    """Carry safe page context from one category attempt to the batch loop."""

    def __init__(
        self,
        *,
        cause: CollectorError,
        failed_page: int | None,
        response_body: bytes | None,
        exception_type: str,
        category_started: bool,
    ) -> None:
        """Preserve only fields approved for local failure handling."""

        super().__init__(str(cause))
        # cause 不包含请求 URL、Cookie 或原始异常文本。
        self.cause = cause
        # failed_page 允许为空，表示分类尚未进入分页请求。
        self.failed_page = failed_page
        # response_body 只会进入 runtime 下受限失败材料。
        self.response_body = response_body
        # exception_type 仅记录类名，避免泄露异常正文。
        self.exception_type = exception_type
        # category_started 决定批次终止事务是否需要收口当前 running 分类。
        self.category_started = category_started


def _collect_category_run(
    *, prepared_batch, task, plan, client, database, runtime_logger, control, retry_round=0
):
    """Persist validated adapter pages without browser or response-format knowledge."""
    # 失败上下文只记录当前页和是否已提交分类开始事务。
    started = False
    failed_page = None
    # 失败退出时显式关闭迭代器，释放适配器当前响应等待。
    iterator = None
    try:
        _raise_if_stopped(control)
        if retry_round:
            # 归档上轮材料后复用分类身份；数据库开始事务会重置失败分类统计。
            prepared_batch.storage.archive_category_attempt(plan.category_run_id, retry_round)
        # 开始时间在数据库和返回结果间保持一致。
        started_at = datetime.now(SHANGHAI_TIMEZONE)
        snapshot = database.start_category_run(
            category_run_id=plan.category_run_id, started_at=started_at
        )
        started = True
        _sync_collection_snapshot(prepared_batch.storage, snapshot)
        _safe_emit(
            runtime_logger,
            level="INFO",
            event="category_collection_started",
            message=(f"[{task.id}] "
                     + (f"补采第 {retry_round}/{TAOBAO_CATEGORY_RETRY_ROUNDS} 轮：" if retry_round else "开始采集 ")
                     + plan.category.display_path),
            stage="category_collection",
            context=LogContext(
                batch_id=prepared_batch.batch_id,
                task_id=task.id,
                category_run_id=plan.category_run_id,
            ),
            details={
                "category_id": plan.category.key,
                "category_path": plan.category.display_path,
                "discovery_order": plan.category.discovery_order,
            },
        )
        # 只接收适配器已校验的记录，平台算法不进入共享编排。
        entries = []
        raw_pages = []
        total = None
        target_pages = None
        iterator = iter(
            client.collect_scope(task, plan.category, prepared_batch.business_date)
        )
        while True:
            _raise_if_stopped(control)
            # 声明的末页已完成后 next() 只触发整体校验，不存在下一页请求。
            expected_page = len(raw_pages) + 1
            failed_page = expected_page if target_pages is None or expected_page <= target_pages else None
            try:
                page = next(iterator)
            except StopIteration:
                break
            # 若适配器仍返回多余页，失败属于该真实返回页，不能归为整体校验。
            failed_page = expected_page
            if page.page_no != expected_page or (
                total is not None and page.api_total != total
            ):
                raise ResponseContractError(
                    "Adapter page sequence changed", category="adapter_page_mismatch"
                )
            if target_pages is not None and page.target_page_count != target_pages:
                raise ResponseContractError(
                    "Adapter pagination plan changed",
                    category="pagination_plan_changed",
                )
            # 审计元数据必须独立于认证信息，第三方适配器也不能传入敏感键。
            if any(
                any(
                    marker in key.lower()
                    for marker in (
                        "cookie",
                        "authorization",
                        "token",
                        "signature",
                        "a_bogus",
                        "verifyfp",
                        "verify_fp",
                    )
                )
                for key in page.safe_params
            ):
                raise ResponseContractError(
                    "Adapter supplied unsafe request metadata",
                    category="unsafe_request_metadata",
                )
            total = page.api_total
            target_pages = page.target_page_count
            # 已验证且返回给编排的页完成审计后再响应停止。
            # 写入顺序保持 raw -> PostgreSQL -> Manifest，未提交页不加入完整结果。
            path = prepared_batch.storage.write_category_page(
                plan.category_run_id, page.page_no, page.payload
            )
            raw_page = RawPageRecord(
                page.page_no,
                path,
                len(page.entries),
                page.captured_at,
                page.safe_params,
            )
            snapshot = database.record_category_page(
                category_run_id=plan.category_run_id,
                raw_page=raw_page,
                api_total=total,
                target_page_count=target_pages,
            )
            _sync_collection_snapshot(prepared_batch.storage, snapshot)
            raw_pages.append(raw_page)
            entries.extend(page.entries)
            _safe_emit(
                runtime_logger,
                level="INFO",
                event="category_page_saved",
                message=f"[{task.id}] 第 {page.page_no}/{target_pages} 页已保存",
                stage="category_collection",
                context=LogContext(
                    batch_id=prepared_batch.batch_id,
                    task_id=task.id,
                    category_run_id=plan.category_run_id,
                ),
                details={
                    "category_id": plan.category.key,
                    "page_no": page.page_no,
                    "saved_items": len(entries),
                    "target_pages": target_pages,
                },
            )
        if total is None or len(raw_pages) != target_pages or len(entries) != total:
            raise ResponseContractError(
                "Adapter returned incomplete scope", category="incomplete_ranking"
            )
        _raise_if_stopped(control)
        # 迭代器完成意味着平台整榜校验也已执行，之后才能提交分类成功。
        finished_at = datetime.now(SHANGHAI_TIMEZONE)
        snapshot = database.finish_category_success(
            category_run_id=plan.category_run_id,
            api_total=total,
            target_page_count=target_pages,
            finished_at=finished_at,
        )
        _sync_collection_snapshot(prepared_batch.storage, snapshot)
        _safe_emit(
            runtime_logger,
            level="INFO",
            event="category_collection_succeeded",
            message=f"[{task.id}] 分类采集完成，共 {total} 条",
            stage="category_collection",
            context=LogContext(
                batch_id=prepared_batch.batch_id,
                task_id=task.id,
                category_run_id=plan.category_run_id,
            ),
            details={
                "category_id": plan.category.key,
                "saved_items": total,
                "target_pages": target_pages,
            },
        )
        return CollectedCategoryRun(
            plan,
            started_at,
            finished_at,
            total,
            target_pages,
            tuple(raw_pages),
            tuple(entries),
        )
    except BaseException as error:
        # Ctrl-C、业务失败和内部错误均进入既有批次收口，不能遗留 running。
        if isinstance(error, KeyboardInterrupt):
            cause = CollectionInterruptedError(
                "Collection interrupted", category="interrupted"
            )
        elif isinstance(error, CollectorError):
            cause = error
        else:
            cause = CollectorError(
                "Unexpected category collection failure", category="internal_error"
            )
        raise _CategoryAttemptFailed(
            cause=cause,
            failed_page=failed_page,
            response_body=cause.response_body,
            exception_type=type(error).__name__,
            category_started=started,
        ) from error
    finally:
        if iterator is not None and hasattr(iterator, "close"):
            iterator.close()


def _save_category_failure(
    *,
    prepared_batch: PreparedCategoryBatch,
    task: TaskConfig,
    plan: CategoryRunPlan,
    failure: _CategoryAttemptFailed,
) -> None:
    """Best-effort save one bounded category failure under runtime only."""

    # 普通接口与契约错误使用更精确的失败步骤标签。
    failed_step = (
        "product_rank_request_or_contract"
        if isinstance(failure.cause, ORDINARY_CATEGORY_ERRORS)
        or isinstance(failure.cause, AuthRequiredError)
        else "product_rank_collection"
    )
    try:
        prepared_batch.storage.save_category_failure(
            category_run_id=plan.category_run_id,
            failed_page=failure.failed_page,
            status_code=failure.cause.status_code,
            error_category=failure.cause.category,
            response_body=failure.response_body,
            failed_step=(failure.cause.failed_step if isinstance(failure.cause, BrowserOperationError)
                         else failed_step),
            exception_type=(failure.cause.exception_type if isinstance(failure.cause, BrowserOperationError)
                            else failure.exception_type),
            safe_endpoint_path=None,
        )
        if isinstance(failure.cause, BrowserOperationError):
            # 浏览器致命错误仍保留已有安全截图与具体步骤，不能在包装后丢失诊断材料。
            prepared_batch.storage.save_browser_failure(
                error_category=failure.cause.category, failed_step=failure.cause.failed_step,
                exception_type=failure.cause.exception_type,
                safe_page_path=failure.cause.safe_page_path, page_title=failure.cause.page_title,
                screenshot=failure.cause.screenshot,
            )
    except Exception:
        # 诊断材料不可写不能覆盖 PostgreSQL 中已经决定的生命周期。
        pass


def _terminate_batch_and_raise(
    *,
    prepared_batch: PreparedCategoryBatch,
    plan: CategoryRunPlan | None,
    failure: _CategoryAttemptFailed,
    status: str,
    database: Database,
    runtime_logger: RuntimeLogger,
    completed_category_runs: list[CollectedCategoryRun],
) -> None:
    """Atomically terminate current and pending categories, then raise safely."""

    # 批次终止时间由 PostgreSQL 和 Manifest 共享。
    finished_at = datetime.now(SHANGHAI_TIMEZONE)
    # 只有已经进入 running 的分类才交给终止事务收口。
    current_category_run_id = (
        plan.category_run_id if plan is not None and failure.category_started else None
    )
    try:
        terminal_snapshot = database.terminate_collection_batch(
            batch_id=prepared_batch.batch_id,
            status=status,
            error_category=failure.cause.category,
            finished_at=finished_at,
            current_category_run_id=current_category_run_id,
            failed_page=failure.failed_page if current_category_run_id else None,
        )
        _sync_collection_snapshot(prepared_batch.storage, terminal_snapshot)
    except Exception as error:
        # 无法完成权威终止事务时改为稳定内部错误，不泄露底层异常正文。
        safe_error = CollectorError(
            "Could not finalize the terminated collection batch",
            category="internal_error",
        )
        raise CategoryBatchCollectionError(
            safe_error,
            prepared_batch.storage,
            tuple(completed_category_runs),
        ) from error
    _safe_emit(
        runtime_logger,
        level="WARNING" if status == "interrupted" else "ERROR",
        event="category_batch_collection_terminated",
        message=(
            f"[{prepared_batch.task_id}] 分类榜单采集终止，"
            f"category={failure.cause.category}"
        ),
        stage="category_collection",
        context=LogContext(
            batch_id=prepared_batch.batch_id,
            task_id=prepared_batch.task_id,
            category_run_id=current_category_run_id,
        ),
        details={
            "batch_status": status,
            "error_category": failure.cause.category,
            "status_code": failure.cause.status_code,
        },
    )
    raise CategoryBatchCollectionError(
        failure.cause,
        prepared_batch.storage,
        tuple(completed_category_runs),
    ) from failure


def _interrupt_before_next_category(
    *,
    prepared_batch: PreparedCategoryBatch,
    database: Database,
    runtime_logger: RuntimeLogger,
    completed_category_runs: list[CollectedCategoryRun],
) -> None:
    """Terminate a batch stopped while no category is currently running."""

    # 边界中止没有失败页、响应正文或当前 running 分类。
    interrupted_error = CollectionInterruptedError(
        "Collection interrupted before next category",
        category="interrupted",
    )
    # 内部失败上下文复用统一批次终止路径。
    failure = _CategoryAttemptFailed(
        cause=interrupted_error,
        failed_page=None,
        response_body=None,
        exception_type=type(interrupted_error).__name__,
        category_started=False,
    )
    _terminate_batch_and_raise(
        prepared_batch=prepared_batch,
        plan=None,
        failure=failure,
        status="interrupted",
        database=database,
        runtime_logger=runtime_logger,
        completed_category_runs=completed_category_runs,
    )


def _category_attempts(plans, failed_ids, platform):
    """完整首轮后，仅按发现顺序生成上一轮仍失败的淘宝分类。"""
    # 集合由消费方更新，每轮快照避免本轮失败被即时重试。
    retry_rounds = TAOBAO_CATEGORY_RETRY_ROUNDS if platform == "taobao" else 0
    for retry_round in range(retry_rounds + 1):
        # 首轮遍历所有计划，后续只保留当前失败分类。
        pending = [plan for plan in plans if retry_round == 0 or plan.category_run_id in failed_ids]
        for plan in pending:
            yield plan, retry_round


def collect_category_batch(
    *,
    prepared_batch: PreparedCategoryBatch,
    task: TaskConfig,
    client: PlatformAdapter,
    database: Database,
    runtime_logger: RuntimeLogger,
    control: CollectionControl | None = None,
) -> CollectedCategoryBatch:
    """Collect planned categories and stop before publication."""

    if task.id != prepared_batch.task_id:
        raise ValueError("task does not match prepared category batch")
    # 成功列表只接收完整验证并已标记 success 的分类。
    completed_category_runs: list[CollectedCategoryRun] = []
    # 普通失败计数用于部分成功发布汇总，不再因少量异常中止任务。
    failed_category_count = 0
    # 最终失败按分类去重，补采成功时移除，不能累计失败尝试次数。
    failed_category_ids = set()
    # last_ordinary_failure 在全部分类失败时提供批次终态原因。
    last_ordinary_failure: tuple[CategoryRunPlan, _CategoryAttemptFailed] | None = None
    # 连续三次平台暂时不可用终止本批，不增加页面请求压力。
    consecutive_platform_unavailable_failures = 0

    for plan, retry_round in _category_attempts(
        prepared_batch.category_run_plans, failed_category_ids, task.platform
    ):
        if control is not None and control.stop_requested():
            _interrupt_before_next_category(
                prepared_batch=prepared_batch,
                database=database,
                runtime_logger=runtime_logger,
                completed_category_runs=completed_category_runs,
            )
        try:
            # 同步函数完整结束一个分类后才会进入下一个分类。
            collected_run = _collect_category_run(
                prepared_batch=prepared_batch,
                task=task,
                plan=plan,
                client=client,
                database=database,
                runtime_logger=runtime_logger,
                control=control,
                retry_round=retry_round,
            )
        except _CategoryAttemptFailed as failure:
            _save_category_failure(
                prepared_batch=prepared_batch,
                task=task,
                plan=plan,
                failure=failure,
            )
            if isinstance(failure.cause, AuthRequiredError):
                _terminate_batch_and_raise(
                    prepared_batch=prepared_batch,
                    plan=plan,
                    failure=failure,
                    status="auth_required",
                    database=database,
                    runtime_logger=runtime_logger,
                    completed_category_runs=completed_category_runs,
                )
            if isinstance(failure.cause, CollectionInterruptedError):
                _terminate_batch_and_raise(
                    prepared_batch=prepared_batch,
                    plan=plan,
                    failure=failure,
                    status="interrupted",
                    database=database,
                    runtime_logger=runtime_logger,
                    completed_category_runs=completed_category_runs,
                )
            if (isinstance(failure.cause, ORDINARY_CATEGORY_ERRORS)
                    or (task.platform == "taobao" and isinstance(failure.cause, BrowserOperationError))):
                # 单分类请求或数据契约异常留档后跳过，批次继续采集其他分类。
                failed_category_ids.add(plan.category_run_id)
                failed_category_count = len(failed_category_ids)
                last_ordinary_failure = (plan, failure)
                is_category_unavailable = (
                    failure.cause.category == "category_unavailable"
                )
                is_platform_temporarily_unavailable = (
                    (task.platform == "compass" and failure.cause.category
                     == PLATFORM_TEMPORARY_UNAVAILABLE_ERROR_CATEGORY)
                )
                # 其他失败会中断当前平台业务错误序列，不能把网络超时累计为平台业务错误。
                consecutive_platform_unavailable_failures = (
                    consecutive_platform_unavailable_failures + 1
                    if is_platform_temporarily_unavailable
                    else 0
                )
                _safe_emit(
                    runtime_logger,
                    level="WARNING" if is_category_unavailable else "ERROR",
                    event=(
                        "category_unavailable"
                        if is_category_unavailable
                        else "category_collection_failed"
                    ),
                    message=(
                        f"[{task.id}] {plan.category.display_path} "
                        + (
                            "当前账号无权访问，已跳过"
                            if is_category_unavailable
                            else f"采集失败，category={failure.cause.category}"
                        )
                    ),
                    stage="category_collection",
                    context=LogContext(
                        batch_id=prepared_batch.batch_id,
                        task_id=task.id,
                        category_run_id=plan.category_run_id,
                    ),
                    details={
                        "category_id": plan.category.category_id,
                        "error_category": failure.cause.category,
                        "page_no": failure.failed_page,
                        "status_code": failure.cause.status_code,
                    },
                )
                try:
                    # 每个普通失败独立收口当前分类，批次继续保持 running。
                    failure_committed = False
                    failure_snapshot = database.finish_category_failure(
                        category_run_id=plan.category_run_id,
                        failed_page=failure.failed_page,
                        error_category=failure.cause.category,
                        finished_at=datetime.now(SHANGHAI_TIMEZONE),
                    )
                    # PostgreSQL 返回快照即表示当前分类已经离开 running。
                    failure_committed = True
                    _sync_collection_snapshot(
                        prepared_batch.storage,
                        failure_snapshot,
                    )
                except Exception as error:
                    # 生命周期持久化失败属于内部错误，不能继续采集后续分类。
                    internal_error = CollectorError(
                        "Could not finalize a failed category run",
                        category="internal_error",
                    )
                    internal_failure = _CategoryAttemptFailed(
                        cause=internal_error,
                        failed_page=failure.failed_page,
                        response_body=None,
                        exception_type=type(error).__name__,
                        category_started=(
                            failure.category_started and not failure_committed
                        ),
                    )
                    _terminate_batch_and_raise(
                        prepared_batch=prepared_batch,
                        plan=plan,
                        failure=internal_failure,
                        status="abandoned",
                        database=database,
                        runtime_logger=runtime_logger,
                        completed_category_runs=completed_category_runs,
                    )
                if (
                    consecutive_platform_unavailable_failures
                    >= MAX_CONSECUTIVE_PLATFORM_UNAVAILABLE_FAILURES
                ):
                    _safe_emit(
                        runtime_logger,
                        level="ERROR",
                        event="platform_unavailable_circuit_opened",
                        message=(
                            f"[{task.id}] 平台连续返回 "
                            f"{'11001' if task.platform == 'compass' else TAOBAO_BUSINESS_ERROR_CATEGORY} 达到 "
                            f"{MAX_CONSECUTIVE_PLATFORM_UNAVAILABLE_FAILURES} 次，"
                            "停止本批采集"
                        ),
                        stage="category_collection",
                        context=LogContext(
                            batch_id=prepared_batch.batch_id,
                            task_id=task.id,
                            category_run_id=plan.category_run_id,
                        ),
                        details={
                            "error_category": failure.cause.category,
                            "consecutive_failure_count": (
                                consecutive_platform_unavailable_failures
                            ),
                        },
                    )
                    _terminate_batch_and_raise(
                        prepared_batch=prepared_batch,
                        plan=plan,
                        failure=failure,
                        status="failed",
                        database=database,
                        runtime_logger=runtime_logger,
                        completed_category_runs=completed_category_runs,
                    )
                continue
            # 数据库、Manifest 或未知 CollectorError 都不能按普通接口失败跳过。
            _terminate_batch_and_raise(
                prepared_batch=prepared_batch,
                plan=plan,
                failure=failure,
                status="abandoned",
                database=database,
                runtime_logger=runtime_logger,
                completed_category_runs=completed_category_runs,
            )
        completed_category_runs.append(collected_run)
        failed_category_ids.discard(plan.category_run_id)
        failed_category_count = len(failed_category_ids)
        # 任何成功分类都会中断当前平台连续业务失败序列。
        consecutive_platform_unavailable_failures = 0

    if control is not None and control.stop_requested():
        _interrupt_before_next_category(
            prepared_batch=prepared_batch,
            database=database,
            runtime_logger=runtime_logger,
            completed_category_runs=completed_category_runs,
        )
    if not completed_category_runs and last_ordinary_failure is not None:
        # 全部分类失败时禁止发布空结果，批次以最后一个稳定原因结束。
        # 元组仅保留失败对象，分类已全部收口，无需标记某个当前分类。
        last_failure = last_ordinary_failure[1]
        _terminate_batch_and_raise(
            prepared_batch=prepared_batch,
            plan=None,
            failure=last_failure,
            status="failed",
            database=database,
            runtime_logger=runtime_logger,
            completed_category_runs=completed_category_runs,
        )
    # 补采完成后恢复分类发现顺序，发布结果不受成功时间影响。
    completed_category_runs.sort(key=lambda run: run.plan.category.discovery_order)
    # 阶段三完成时间不终结数据库批次，后续阶段仍负责正式发布。
    finished_at = datetime.now(SHANGHAI_TIMEZONE)
    # 成功分类商品数仅用于允许字段的批次准备日志。
    saved_item_count = sum(
        len(category_run.entries) for category_run in completed_category_runs
    )
    _safe_emit(
        runtime_logger,
        level="INFO",
        event="category_batch_collection_ready",
        message=(
            f"[{task.id}] 分类榜单采集完成：成功 {len(completed_category_runs)}，"
            f"失败 {failed_category_count}，等待发布阶段"
        ),
        stage="category_collection",
        context=LogContext(batch_id=prepared_batch.batch_id, task_id=task.id),
        details={
            "discovered_category_count": len(prepared_batch.category_run_plans),
            "saved_items": saved_item_count,
        },
    )
    return CollectedCategoryBatch(
        batch_id=prepared_batch.batch_id,
        task_id=prepared_batch.task_id,
        business_date=prepared_batch.business_date,
        started_at=prepared_batch.started_at,
        finished_at=finished_at,
        storage=prepared_batch.storage,
        category_runs=tuple(completed_category_runs),
        failed_category_count=failed_category_count,
    )
