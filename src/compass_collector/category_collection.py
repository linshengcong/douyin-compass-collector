"""Collect every discovered level-three ranking without publishing a batch."""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from compass_collector.category_batch import PreparedCategoryBatch
from compass_collector.config import TaskConfig
from compass_collector.errors import (
    AuthRequiredError,
    CategoryBatchCollectionError,
    CollectionInterruptedError,
    CollectorError,
    HttpRequestError,
    HttpResponseError,
    ResponseContractError,
)
from compass_collector.http_client import CompassHttpClient, HttpJsonResponse
from compass_collector.models import (
    CategoryRunPlan,
    CollectedCategoryBatch,
    CollectedCategoryRun,
    ProductRankEntry,
    RawPageRecord,
)
from compass_collector.persistence import Database
from compass_collector.product_rank import (
    build_request_params,
    calculate_pagination_plan,
    parse_page_entries,
    validate_complete_ranking,
    validate_page_payload,
)
from compass_collector.run_control import CollectionControl
from compass_collector.runtime_logging import LogContext, RuntimeLogger


# 采集和 Manifest 时间统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 第三个普通分类失败会终止整个批次，前两个允许后续正式发布部分结果。
ORDINARY_FAILURE_LIMIT = 3
# 只有网络、HTTP 和已验证响应契约错误可以跳过当前分类继续执行。
ORDINARY_CATEGORY_ERRORS = (
    HttpRequestError,
    HttpResponseError,
    ResponseContractError,
)
# 分页预取从第二页开始，第一页必须先串行建立 total 和目标页数。
FIRST_PREFETCH_PAGE_NO = 2


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


class _FetchedPageFailed(Exception):
    """Attach the page number to one failed concurrent page request."""

    def __init__(self, page_no: int, cause: BaseException) -> None:
        """Keep the original failure while exposing its requested page."""

        super().__init__(str(cause))
        # page_no 用于上层记录失败页，避免并发完成顺序影响诊断。
        self.page_no = page_no
        # cause 交给主线程按既有 CollectorError 规则处理。
        self.cause = cause


def _safe_emit(runtime_logger: RuntimeLogger, **event_fields: Any) -> None:
    """Keep diagnostic logging failures from changing collection semantics."""

    try:
        runtime_logger.emit(**event_fields)
    except Exception:
        # SQLite 和 Manifest 是权威状态，日志不可用不能覆盖业务结果。
        pass


def _sync_collection_snapshot(storage: Any, snapshot: Any) -> None:
    """Retry one transient Manifest projection without repeating SQLite writes."""

    # 同一权威快照最多原位同步两次，绝不重复提交数据库事务。
    for sync_attempt in range(2):
        try:
            storage.sync_collection_snapshot(snapshot)
            return
        except Exception:
            if sync_attempt == 1:
                raise


def _raise_if_stopped(control: CollectionControl | None) -> None:
    """Convert one cooperative stop signal into the stable collector error."""

    if control is not None and control.stop_requested():
        raise CollectionInterruptedError(
            "Collection interrupted by user request",
            category="interrupted",
        )


def _page_fetch_workers(client: CompassHttpClient) -> int:
    """Return the page prefetch worker count exposed by the HTTP client."""

    # 测试替身可能只实现 get_product_rank_page，默认回退到串行语义。
    return max(1, int(getattr(client, "page_fetch_workers", 1)))


def _failure_response_body(
    cause: CollectorError,
    latest_response_body: bytes | None,
) -> bytes | None:
    """Prefer the error-owned response while retaining contract-failure bodies."""

    # HTTP 客户端错误已携带确切失败响应，契约错误则使用本页最新响应。
    return cause.response_body if cause.response_body is not None else latest_response_body


def _collect_category_run(
    *,
    prepared_batch: PreparedCategoryBatch,
    task: TaskConfig,
    plan: CategoryRunPlan,
    client: CompassHttpClient,
    database: Database,
    runtime_logger: RuntimeLogger,
    control: CollectionControl | None,
) -> CollectedCategoryRun:
    """Collect and validate one category through its dynamically calculated last page."""

    # 分类只有在 start_category_run 事务成功后才属于 running 状态。
    category_started = False
    # 页码在进入第一页请求前保持为空，便于区分启动阶段故障。
    failed_page: int | None = None
    # 当前响应正文仅用于本页契约失败时的 runtime 诊断材料。
    latest_response_body: bytes | None = None
    try:
        _raise_if_stopped(control)
        # 分类开始时间只计算一次并同时写入 SQLite 和结果模型。
        started_at = datetime.now(SHANGHAI_TIMEZONE)
        # 分类生命周期先进入 SQLite，再同步同一个 Manifest 镜像。
        start_snapshot = database.start_category_run(
            category_run_id=plan.category_run_id,
            started_at=started_at,
        )
        category_started = True
        _sync_collection_snapshot(prepared_batch.storage, start_snapshot)
        _safe_emit(
            runtime_logger,
            level="INFO",
            event="category_collection_started",
            message=f"[{task.id}] 开始采集 {plan.category.display_path}",
            stage="category_collection",
            context=LogContext(
                batch_id=prepared_batch.batch_id,
                task_id=task.id,
                category_run_id=plan.category_run_id,
            ),
            details={
                "category_id": plan.category.category_id,
                "category_path": plan.category.display_path,
                "discovery_order": plan.category.discovery_order,
            },
        )

        # entries 只在每页完成三层持久化后追加。
        entries: list[ProductRankEntry] = []
        # raw_pages 与 SQLite raw_responses 保持相同顺序。
        raw_pages: list[RawPageRecord] = []
        # api_total 只从第一页建立，后续页面必须保持一致。
        api_total: int | None = None
        # target_page_count 由真实 total 动态计算，不设置 200 条上限。
        target_page_count: int | None = None

        def fetch_page(page_no: int) -> HttpJsonResponse:
            """Fetch one ranking page without mutating local collection state."""

            # worker 线程只构造请求并等待 HTTP 响应，不写 SQLite 或 raw。
            request_params = build_request_params(
                task=task,
                category=plan.category,
                business_date=prepared_batch.business_date,
                page_no=page_no,
            )
            return client.get_product_rank_page(task, request_params)

        def persist_page(
            *,
            page_no: int,
            response: HttpJsonResponse,
        ) -> None:
            """Validate and persist one already fetched page in page-number order."""

            nonlocal api_total, target_page_count, latest_response_body

            # 当前响应正文仅在本页契约失败时进入失败材料。
            latest_response_body = response.body
            # 捕获时间在完整响应收到后计算，并贯穿 raw 索引和商品条目。
            captured_at = datetime.now(SHANGHAI_TIMEZONE)
            # 后续页传入首屏 total，拒绝实时榜单分页过程中总数漂移。
            page_contract = validate_page_payload(
                response.payload,
                requested_page=page_no,
                expected_total=api_total,
            )
            if api_total is None:
                # 第一页 total 同时决定目标条数和页数，total=0 仍为一页。
                pagination_plan = calculate_pagination_plan(page_contract.api_total)
                api_total = pagination_plan.api_total
                target_page_count = pagination_plan.target_page_count
            if (
                target_page_count is None
                or page_contract.target_page_count != target_page_count
            ):
                raise ResponseContractError(
                    "response target page count changed during pagination",
                    category="pagination_plan_changed",
                )
            # 商品解析必须在正式 raw 页发布前全部通过字段契约。
            page_entries = parse_page_entries(
                response.payload,
                page_no=page_no,
                captured_at=captured_at,
            )
            # 正式页严格执行 raw gzip -> SQLite 索引 -> Manifest 镜像。
            page_path = prepared_batch.storage.write_category_page(
                plan.category_run_id,
                page_no,
                response.payload,
            )
            # raw_page 是 SQLite 和成功结果共享的不可变页级索引。
            raw_page = RawPageRecord(
                page_no=page_no,
                path=page_path,
                item_count=page_contract.item_count,
                captured_at=captured_at,
            )
            # 此处两个可选值已由第一页契约建立，断言只帮助类型收窄。
            assert api_total is not None
            assert target_page_count is not None
            page_snapshot = database.record_category_page(
                category_run_id=plan.category_run_id,
                raw_page=raw_page,
                api_total=api_total,
                target_page_count=target_page_count,
            )
            _sync_collection_snapshot(prepared_batch.storage, page_snapshot)
            # 只有三层持久化全部完成的页才进入最终完整榜单校验。
            raw_pages.append(raw_page)
            entries.extend(page_entries)
            _safe_emit(
                runtime_logger,
                level="INFO",
                event="category_page_saved",
                message=(
                    f"[{task.id}] {plan.category.category_name} "
                    f"已保存第 {page_no}/{target_page_count} 页"
                ),
                stage="category_collection",
                context=LogContext(
                    batch_id=prepared_batch.batch_id,
                    task_id=task.id,
                    category_run_id=plan.category_run_id,
                ),
                details={
                    "category_id": plan.category.category_id,
                    "page_no": page_no,
                    "saved_items": len(entries),
                    "target_pages": target_page_count,
                },
            )
            # 已保存的正常响应不复制到人工中止失败材料。
            latest_response_body = None

        # 第一页是所有分类都必须请求的入口，包括 total=0。
        page_no = 1
        latest_response_body = None
        failed_page = page_no
        _raise_if_stopped(control)
        persist_page(page_no=page_no, response=fetch_page(page_no))
        _raise_if_stopped(control)
        # 第一页成功后 target_page_count 必然已经由响应契约建立。
        assert target_page_count is not None

        if target_page_count >= FIRST_PREFETCH_PAGE_NO:
            # page_fetch_workers 是本分类后续分页预取的实际并发度。
            page_fetch_workers = _page_fetch_workers(client)
            # next_page_to_submit 是尚未进入线程池的下一页。
            next_page_to_submit = FIRST_PREFETCH_PAGE_NO
            # next_page_to_persist 是主线程下一次允许落盘的页。
            next_page_to_persist = FIRST_PREFETCH_PAGE_NO
            # fetched_pages 暂存乱序完成的响应，直到轮到对应页持久化。
            fetched_pages: dict[int, HttpJsonResponse] = {}
            # futures 持有页码映射，避免线程完成顺序影响失败定位。
            futures: dict[Future[HttpJsonResponse], int] = {}

            with ThreadPoolExecutor(max_workers=page_fetch_workers) as executor:
                while (
                    next_page_to_submit <= target_page_count
                    and len(futures) < page_fetch_workers
                ):
                    _raise_if_stopped(control)
                    futures[executor.submit(fetch_page, next_page_to_submit)] = (
                        next_page_to_submit
                    )
                    next_page_to_submit += 1

                while next_page_to_persist <= target_page_count:
                    _raise_if_stopped(control)
                    if next_page_to_persist not in fetched_pages:
                        done, _pending = wait(
                            futures.keys(),
                            return_when=FIRST_COMPLETED,
                        )
                        for future in done:
                            completed_page_no = futures.pop(future)
                            try:
                                fetched_pages[completed_page_no] = future.result()
                            except BaseException as error:
                                # 失败页按请求页码记录，不受并发完成顺序影响。
                                raise _FetchedPageFailed(
                                    completed_page_no,
                                    error,
                                ) from error
                            while (
                                next_page_to_submit <= target_page_count
                                and len(futures) < page_fetch_workers
                            ):
                                _raise_if_stopped(control)
                                future = executor.submit(fetch_page, next_page_to_submit)
                                futures[future] = next_page_to_submit
                                next_page_to_submit += 1
                        continue

                    # 只有主线程按页码顺序持久化，数据库连续页约束保持不变。
                    failed_page = next_page_to_persist
                    persist_page(
                        page_no=next_page_to_persist,
                        response=fetched_pages.pop(next_page_to_persist),
                    )
                    _raise_if_stopped(control)
                    next_page_to_persist += 1

        # 完整分类必须覆盖 1..api_total 且无重复商品或排名。
        validate_complete_ranking(entries, api_total=api_total)
        _raise_if_stopped(control)
        # 分类完成时间只在整榜校验成功后生成。
        finished_at = datetime.now(SHANGHAI_TIMEZONE)
        success_snapshot = database.finish_category_success(
            category_run_id=plan.category_run_id,
            api_total=api_total,
            target_page_count=target_page_count,
            finished_at=finished_at,
        )
        # SQLite 已提交 success 后当前分类不再属于 running。
        category_started = False
        _sync_collection_snapshot(prepared_batch.storage, success_snapshot)
        _safe_emit(
            runtime_logger,
            level="INFO",
            event="category_collection_succeeded",
            message=(
                f"[{task.id}] {plan.category.display_path} 采集完成，"
                f"共 {api_total} 条"
            ),
            stage="category_collection",
            context=LogContext(
                batch_id=prepared_batch.batch_id,
                task_id=task.id,
                category_run_id=plan.category_run_id,
            ),
            details={
                "category_id": plan.category.category_id,
                "saved_items": api_total,
                "target_pages": target_page_count,
            },
        )
        return CollectedCategoryRun(
            plan=plan,
            started_at=started_at,
            finished_at=finished_at,
            api_total=api_total,
            target_page_count=target_page_count,
            raw_pages=tuple(raw_pages),
            entries=tuple(entries),
        )
    except KeyboardInterrupt as error:
        # 终端 Ctrl-C 与 GUI 停止使用相同 interrupted 生命周期。
        safe_error = CollectionInterruptedError(
            "Collection interrupted from terminal",
            category="interrupted",
        )
        raise _CategoryAttemptFailed(
            cause=safe_error,
            failed_page=failed_page,
            response_body=None,
            exception_type=type(error).__name__,
            category_started=category_started,
        ) from error
    except _FetchedPageFailed as error:
        if isinstance(error.cause, CollectorError):
            # HTTP 或协作式中断失败沿用原有错误分类。
            response_body = _failure_response_body(error.cause, latest_response_body)
            raise _CategoryAttemptFailed(
                cause=error.cause,
                failed_page=error.page_no,
                response_body=response_body,
                exception_type=type(error.cause).__name__,
                category_started=category_started,
            ) from error
        # 线程池中的非业务异常统一转换为安全内部错误。
        safe_error = CollectorError(
            "Unexpected category page fetch failure",
            category="internal_error",
        )
        raise _CategoryAttemptFailed(
            cause=safe_error,
            failed_page=error.page_no,
            response_body=latest_response_body,
            exception_type=type(error.cause).__name__,
            category_started=category_started,
        ) from error
    except CollectorError as error:
        # HTTP 错误自带响应优先，契约错误使用当前已收到的响应。
        response_body = _failure_response_body(error, latest_response_body)
        raise _CategoryAttemptFailed(
            cause=error,
            failed_page=failed_page,
            response_body=response_body,
            exception_type=type(error).__name__,
            category_started=category_started,
        ) from error
    except Exception as error:
        # 存储、数据库或代码异常统一转换，禁止原始异常文本进入上层。
        safe_error = CollectorError(
            "Unexpected category collection failure",
            category="internal_error",
        )
        raise _CategoryAttemptFailed(
            cause=safe_error,
            failed_page=failed_page,
            response_body=latest_response_body,
            exception_type=type(error).__name__,
            category_started=category_started,
        ) from error


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
            failed_step=failed_step,
            exception_type=failure.exception_type,
            safe_endpoint_path=task.rank.endpoint_path,
        )
    except Exception:
        # 诊断材料不可写不能覆盖 SQLite 中已经决定的生命周期。
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

    # 批次终止时间由 SQLite 和 Manifest 共享。
    finished_at = datetime.now(SHANGHAI_TIMEZONE)
    # 只有已经进入 running 的分类才交给终止事务收口。
    current_category_run_id = (
        plan.category_run_id
        if plan is not None and failure.category_started
        else None
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


def collect_category_batch(
    *,
    prepared_batch: PreparedCategoryBatch,
    task: TaskConfig,
    client: CompassHttpClient,
    database: Database,
    runtime_logger: RuntimeLogger,
    control: CollectionControl | None = None,
) -> CollectedCategoryBatch:
    """Collect all planned categories serially and stop before publication."""

    if task.id != prepared_batch.task_id:
        raise ValueError("task does not match prepared category batch")
    # 成功列表只接收完整验证并已标记 success 的分类。
    completed_category_runs: list[CollectedCategoryRun] = []
    # 普通失败计数在第 3 次触发批次原子终止。
    failed_category_count = 0

    for plan in prepared_batch.category_run_plans:
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
            if isinstance(failure.cause, ORDINARY_CATEGORY_ERRORS):
                # 当前失败先计数，第 3 个不得单独提交后再终止批次。
                failed_category_count += 1
                _safe_emit(
                    runtime_logger,
                    level="ERROR",
                    event="category_collection_failed",
                    message=(
                        f"[{task.id}] {plan.category.display_path} 采集失败，"
                        f"category={failure.cause.category}"
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
                if failed_category_count >= ORDINARY_FAILURE_LIMIT:
                    _terminate_batch_and_raise(
                        prepared_batch=prepared_batch,
                        plan=plan,
                        failure=failure,
                        status="failed",
                        database=database,
                        runtime_logger=runtime_logger,
                        completed_category_runs=completed_category_runs,
                    )
                try:
                    # 第 1、2 个普通失败只收口当前分类，批次继续 running。
                    failure_committed = False
                    failure_snapshot = database.finish_category_failure(
                        category_run_id=plan.category_run_id,
                        failed_page=failure.failed_page,
                        error_category=failure.cause.category,
                        finished_at=datetime.now(SHANGHAI_TIMEZONE),
                    )
                    # SQLite 返回快照即表示当前分类已经离开 running。
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

    if control is not None and control.stop_requested():
        _interrupt_before_next_category(
            prepared_batch=prepared_batch,
            database=database,
            runtime_logger=runtime_logger,
            completed_category_runs=completed_category_runs,
        )
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
