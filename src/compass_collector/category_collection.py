"""Collect every discovered level-three ranking without publishing a batch."""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, Queue
from threading import Event, get_ident
from time import monotonic
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
# 只有网络、HTTP 和已验证响应契约错误可以跳过当前分类继续执行。
ORDINARY_CATEGORY_ERRORS = (
    HttpRequestError,
    HttpResponseError,
    ResponseContractError,
)
# 分页预取从第二页开始，第一页必须先串行建立 total 和目标页数。
FIRST_PREFETCH_PAGE_NO = 2


@dataclass(slots=True)
class _PersistenceRequest:
    """Carry one worker-requested write back to the collection owner thread."""

    # action contains only a local database, raw-storage, Manifest, or log write.
    action: Callable[[], Any]
    # completed unblocks the worker after the owner thread has executed action.
    completed: Event = field(default_factory=Event)
    # result and error preserve the original action outcome for the worker.
    result: Any = None
    error: BaseException | None = None


class _MainThreadPersistence:
    """Serialize collector side effects on the thread that owns batch orchestration."""

    def __init__(self) -> None:
        """Capture the caller thread and initialize its queued work handoff."""

        # owner_thread_id identifies the only thread allowed to touch SQLite and storage.
        self.owner_thread_id = get_ident()
        # requests stays unbounded because every worker waits after placing one action.
        self.requests: Queue[_PersistenceRequest] = Queue()

    def call(self, action: Callable[[], Any]) -> Any:
        """Execute locally for the owner or wait for it to execute a worker action."""

        if get_ident() == self.owner_thread_id:
            return action()
        request = _PersistenceRequest(action=action)
        self.requests.put(request)
        request.completed.wait()
        if request.error is not None:
            raise request.error
        return request.result

    def process_one(self, timeout_seconds: float) -> bool:
        """Run at most one queued side effect from the collection owner thread."""

        if get_ident() != self.owner_thread_id:
            raise RuntimeError("persistence queue must be drained by its owner thread")
        try:
            request = self.requests.get(timeout=timeout_seconds)
        except Empty:
            return False
        try:
            request.result = request.action()
        except BaseException as error:
            request.error = error
        finally:
            request.completed.set()
        return True


@dataclass(slots=True)
class _CategoryOutcome:
    """Hand one completed category attempt to the owner before the group advances."""

    # plan lets the owner apply existing failure and lifecycle rules deterministically.
    plan: CategoryRunPlan
    # collected_run is present only after a complete successful category.
    collected_run: CollectedCategoryRun | None = None
    # failure is present only after the category attempt reached a safe failure boundary.
    failure: "_CategoryAttemptFailed | None" = None
    # continue_event releases the worker only after the owner decides whether to advance.
    continue_event: Event = field(default_factory=Event)
    # should_continue becomes false once a batch-level terminal state has been chosen.
    should_continue: bool = False


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


def _run_persistence(
    persistence: _MainThreadPersistence | None,
    action: Callable[[], Any],
) -> Any:
    """Run one side effect directly or route it to the collection owner thread."""

    # 串行兼容路径不创建队列；一级分类并发路径统一由主线程执行 action。
    return action() if persistence is None else persistence.call(action)


def _safe_emit(
    runtime_logger: RuntimeLogger,
    *,
    persistence: _MainThreadPersistence | None = None,
    **event_fields: Any,
) -> None:
    """Keep diagnostic logging failures from changing collection semantics."""

    try:
        _run_persistence(
            persistence,
            lambda: runtime_logger.emit(**event_fields),
        )
    except Exception:
        # SQLite 和 Manifest 是权威状态，日志不可用不能覆盖业务结果。
        pass


def _sync_collection_snapshot(
    storage: Any,
    snapshot: Any,
    *,
    persistence: _MainThreadPersistence | None = None,
) -> None:
    """Retry one transient Manifest projection without repeating SQLite writes."""

    # 同一权威快照最多原位同步两次，绝不重复提交数据库事务。
    for sync_attempt in range(2):
        try:
            _run_persistence(
                persistence,
                lambda: storage.sync_collection_snapshot(snapshot),
            )
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


def _page_fetch_wait_timeout_seconds(client: CompassHttpClient) -> float:
    """Return the absolute no-progress timeout for concurrent page futures."""

    # 测试替身未暴露该值时使用保守默认值，真实客户端由 HTTP 配置推导。
    return max(0.01, float(getattr(client, "page_fetch_wait_timeout_seconds", 45.0)))


def _wait_for_completed_page_futures(
    *,
    futures: dict[Future[HttpJsonResponse], int],
    timeout_seconds: float,
    check_stopped: Callable[[], None],
) -> set[Future[HttpJsonResponse]]:
    """Wait for one page future while enforcing an absolute no-progress deadline."""

    # 单次短等待允许 GUI 停止请求及时打断，不必等待完整网络截止时间。
    deadline = monotonic() + timeout_seconds
    while True:
        check_stopped()
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            raise HttpRequestError(
                "Concurrent page fetch made no progress before its deadline",
                category="timeout",
            )
        done, _pending = wait(
            futures.keys(),
            timeout=min(0.5, remaining_seconds),
            return_when=FIRST_COMPLETED,
        )
        if done:
            return done


def _level1_fetch_workers(client: CompassHttpClient) -> int:
    """Return the level-one group worker count exposed by the HTTP client."""

    # 测试替身未声明一级并发时，默认沿用原有全串行分类行为。
    return max(1, int(getattr(client, "level1_fetch_workers", 1)))


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
    persistence: _MainThreadPersistence | None = None,
    batch_stop_event: Event | None = None,
) -> CollectedCategoryRun:
    """Collect and validate one category through its dynamically calculated last page."""

    # 分类只有在 start_category_run 事务成功后才属于 running 状态。
    category_started = False
    # 页码在进入第一页请求前保持为空，便于区分启动阶段故障。
    failed_page: int | None = None
    # 当前响应正文仅用于本页契约失败时的 runtime 诊断材料。
    latest_response_body: bytes | None = None

    def raise_if_collection_stopped() -> None:
        """Check both user cancellation and a terminal decision from another group."""

        _raise_if_stopped(control)
        # 并行组一旦出现批次终态，其他组只允许在当前安全边界退出。
        if batch_stop_event is not None and batch_stop_event.is_set():
            raise CollectionInterruptedError(
                "Collection stopped after another category reached a terminal state",
                category="interrupted",
            )

    try:
        raise_if_collection_stopped()
        # 分类开始时间只计算一次并同时写入 SQLite 和结果模型。
        started_at = datetime.now(SHANGHAI_TIMEZONE)
        # 分类生命周期先进入 SQLite，再同步同一个 Manifest 镜像。
        start_snapshot = _run_persistence(
            persistence,
            lambda: database.start_category_run(
                category_run_id=plan.category_run_id,
                started_at=started_at,
            ),
        )
        category_started = True
        _sync_collection_snapshot(
            prepared_batch.storage,
            start_snapshot,
            persistence=persistence,
        )
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
            persistence=persistence,
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
            page_path = _run_persistence(
                persistence,
                lambda: prepared_batch.storage.write_category_page(
                    plan.category_run_id,
                    page_no,
                    response.payload,
                ),
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
            page_snapshot = _run_persistence(
                persistence,
                lambda: database.record_category_page(
                    category_run_id=plan.category_run_id,
                    raw_page=raw_page,
                    api_total=api_total,
                    target_page_count=target_page_count,
                ),
            )
            _sync_collection_snapshot(
                prepared_batch.storage,
                page_snapshot,
                persistence=persistence,
            )
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
                persistence=persistence,
            )
            # 已保存的正常响应不复制到人工中止失败材料。
            latest_response_body = None

        # 第一页是所有分类都必须请求的入口，包括 total=0。
        page_no = 1
        latest_response_body = None
        failed_page = page_no
        raise_if_collection_stopped()
        persist_page(page_no=page_no, response=fetch_page(page_no))
        raise_if_collection_stopped()
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

            # 不使用 context manager：超时路径不能因等待失联 worker 而再次卡住。
            executor = ThreadPoolExecutor(max_workers=page_fetch_workers)
            try:
                while (
                    next_page_to_submit <= target_page_count
                    and len(futures) < page_fetch_workers
                ):
                    raise_if_collection_stopped()
                    futures[executor.submit(fetch_page, next_page_to_submit)] = (
                        next_page_to_submit
                    )
                    next_page_to_submit += 1

                while next_page_to_persist <= target_page_count:
                    raise_if_collection_stopped()
                    if next_page_to_persist not in fetched_pages:
                        try:
                            done = _wait_for_completed_page_futures(
                                futures=futures,
                                timeout_seconds=_page_fetch_wait_timeout_seconds(client),
                                check_stopped=raise_if_collection_stopped,
                            )
                        except HttpRequestError as error:
                            # 取消尚未启动的页；已在运行的请求不阻塞当前分类的失败收口。
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise _FetchedPageFailed(
                                next_page_to_persist,
                                error,
                            ) from error
                        for future in done:
                            completed_page_no = futures.pop(future)
                            try:
                                fetched_pages[completed_page_no] = future.result()
                            except BaseException as error:
                                # 任一页已失败时，不等待其他可能失联的预取请求。
                                executor.shutdown(wait=False, cancel_futures=True)
                                # 失败页按请求页码记录，不受并发完成顺序影响。
                                raise _FetchedPageFailed(
                                    completed_page_no,
                                    error,
                                ) from error
                            while (
                                next_page_to_submit <= target_page_count
                                and len(futures) < page_fetch_workers
                            ):
                                raise_if_collection_stopped()
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
                    raise_if_collection_stopped()
                    next_page_to_persist += 1
            except BaseException:
                # 异常路径取消未开始的请求，不因 executor 默认等待行为卡住整个批次。
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                # 所有页已完成时正常回收 worker，避免后台线程跨分类残留。
                executor.shutdown(wait=True)

        # 完整分类必须覆盖 1..api_total 且无重复商品或排名。
        validate_complete_ranking(entries, api_total=api_total)
        raise_if_collection_stopped()
        # 分类完成时间只在整榜校验成功后生成。
        finished_at = datetime.now(SHANGHAI_TIMEZONE)
        success_snapshot = _run_persistence(
            persistence,
            lambda: database.finish_category_success(
                category_run_id=plan.category_run_id,
                api_total=api_total,
                target_page_count=target_page_count,
                finished_at=finished_at,
            ),
        )
        # SQLite 已提交 success 后当前分类不再属于 running。
        category_started = False
        _sync_collection_snapshot(
            prepared_batch.storage,
            success_snapshot,
            persistence=persistence,
        )
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
            persistence=persistence,
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


def _group_category_plans_by_level1(
    plans: tuple[CategoryRunPlan, ...],
) -> tuple[tuple[CategoryRunPlan, ...], ...]:
    """Keep each level-one category's discovered level-three plans together."""

    # dict 保留首次出现顺序，因而组调度与分类树原始顺序一致。
    grouped_plans: dict[str, list[CategoryRunPlan]] = {}
    for plan in plans:
        grouped_plans.setdefault(plan.category.level1_category_id, []).append(plan)
    return tuple(tuple(group) for group in grouped_plans.values())


def _collect_level1_group(
    *,
    plans: tuple[CategoryRunPlan, ...],
    prepared_batch: PreparedCategoryBatch,
    task: TaskConfig,
    client: CompassHttpClient,
    database: Database,
    runtime_logger: RuntimeLogger,
    control: CollectionControl | None,
    persistence: _MainThreadPersistence,
    batch_stop_event: Event,
    outcome_queue: Queue[_CategoryOutcome],
) -> None:
    """Fetch one level-one group serially while the owner applies each outcome."""

    for plan in plans:
        # 终态确定后不允许本组启动下一个三级分类。
        if batch_stop_event.is_set():
            return
        try:
            collected_run = _collect_category_run(
                prepared_batch=prepared_batch,
                task=task,
                plan=plan,
                client=client,
                database=database,
                runtime_logger=runtime_logger,
                control=control,
                persistence=persistence,
                batch_stop_event=batch_stop_event,
            )
            outcome = _CategoryOutcome(plan=plan, collected_run=collected_run)
        except _CategoryAttemptFailed as failure:
            outcome = _CategoryOutcome(plan=plan, failure=failure)
        outcome_queue.put(outcome)
        # 失败处理和终态只能由 owner 统一决定，避免两个组各自继续。
        outcome.continue_event.wait()
        if not outcome.should_continue:
            return


def _collect_category_batch_by_level1(
    *,
    prepared_batch: PreparedCategoryBatch,
    task: TaskConfig,
    client: CompassHttpClient,
    database: Database,
    runtime_logger: RuntimeLogger,
    control: CollectionControl | None,
    level1_groups: tuple[tuple[CategoryRunPlan, ...], ...],
) -> CollectedCategoryBatch:
    """Collect level-one groups concurrently while retaining one owner for writes."""

    # 成功分类按到达顺序积累，返回和 CSV 前再恢复 discovery_order。
    completed_category_runs: list[CollectedCategoryRun] = []
    # 普通失败在整个批次维度累计，用于 partial_success 汇总。
    failed_category_count = 0
    # last_ordinary_failure 在所有分类均失败时提供稳定的终态原因。
    last_ordinary_failure: tuple[CategoryRunPlan, _CategoryAttemptFailed] | None = None
    # owner 线程执行所有 SQLite、raw、Manifest 和日志副作用。
    persistence = _MainThreadPersistence()
    # stop 信号同时阻止补充新一级分类组和组内启动下一个三级分类。
    batch_stop_event = Event()
    # 每个 outcome 都必须得到主线程许可，防止失败时工作线程越过阈值。
    outcome_queue: Queue[_CategoryOutcome] = Queue()
    # terminal_failure 延后到所有工作线程退出后再执行批次终止事务。
    terminal_failure: tuple[CategoryRunPlan | None, _CategoryAttemptFailed, str] | None = None

    # next_group_index identifies the next discovered一级分类组 that may enter a free slot.
    next_group_index = 0
    # max_group_workers prevents empty pools and enforces the configured group limit.
    max_group_workers = min(client.level1_fetch_workers, len(level1_groups))
    # active_futures tracks groups that can still enqueue persistence work or outcomes.
    active_futures: set[Future[None]] = set()

    def submit_next_group(executor: ThreadPoolExecutor) -> bool:
        """Submit one discovered level-one group when a scheduler slot is free."""

        nonlocal next_group_index
        if next_group_index >= len(level1_groups) or batch_stop_event.is_set():
            return False
        group = level1_groups[next_group_index]
        next_group_index += 1
        active_futures.add(
            executor.submit(
                _collect_level1_group,
                plans=group,
                prepared_batch=prepared_batch,
                task=task,
                client=client,
                database=database,
                runtime_logger=runtime_logger,
                control=control,
                persistence=persistence,
                batch_stop_event=batch_stop_event,
                outcome_queue=outcome_queue,
            )
        )
        return True

    with ThreadPoolExecutor(max_workers=max_group_workers) as executor:
        # 任一一级分类组退出就立即补位；认证失效或中止一经 owner 确认即冻结后续提交。
        while active_futures or (
            terminal_failure is None and next_group_index < len(level1_groups)
        ):
            if not active_futures:
                while len(active_futures) < max_group_workers and submit_next_group(executor):
                    pass
                continue

            # 优先执行已经排队的持久化操作，避免 worker 因等待主线程而饥饿。
            if persistence.process_one(timeout_seconds=0.01):
                continue
            try:
                outcome = outcome_queue.get_nowait()
            except Empty:
                completed_futures = {
                    future for future in active_futures if future.done()
                }
                for future in completed_futures:
                    active_futures.remove(future)
                    try:
                        future.result()
                    except Exception as error:
                        # 未能转换为安全分类失败的 worker 异常必须终止批次。
                        if terminal_failure is None:
                            _safe_emit(
                                runtime_logger,
                                level="ERROR",
                                event="level1_worker_failed",
                                message=(
                                    f"[{task.id}] 一级分类工作线程异常，正在安全收口批次"
                                ),
                                stage="category_collection",
                                context=LogContext(
                                    batch_id=prepared_batch.batch_id,
                                    task_id=task.id,
                                ),
                                details={"exception_type": type(error).__name__},
                            )
                            safe_error = CollectorError(
                                "Unexpected level-one collection worker failure",
                                category="internal_error",
                            )
                            terminal_failure = (
                                None,
                                _CategoryAttemptFailed(
                                    cause=safe_error,
                                    failed_page=None,
                                    response_body=None,
                                    exception_type=type(error).__name__,
                                    category_started=False,
                                ),
                                "abandoned",
                            )
                            batch_stop_event.set()
                # 已完成组释放的槽位立即补充下一组，避免慢组造成长期空闲槽位。
                while (
                    terminal_failure is None
                    and len(active_futures) < max_group_workers
                    and submit_next_group(executor)
                ):
                    pass
                continue

            # 首个终态原因是批次审计依据；后续并行组只释放并退出，不能覆盖它。
            if terminal_failure is not None:
                outcome.should_continue = False
                outcome.continue_event.set()
                continue

            if outcome.collected_run is not None:
                completed_category_runs.append(outcome.collected_run)
                outcome.should_continue = terminal_failure is None
                outcome.continue_event.set()
                continue

            assert outcome.failure is not None
            failure = outcome.failure
            _save_category_failure(
                prepared_batch=prepared_batch,
                task=task,
                plan=outcome.plan,
                failure=failure,
            )
            if isinstance(failure.cause, AuthRequiredError):
                terminal_failure = (outcome.plan, failure, "auth_required")
            elif isinstance(failure.cause, CollectionInterruptedError):
                terminal_failure = (outcome.plan, failure, "interrupted")
            elif isinstance(failure.cause, ORDINARY_CATEGORY_ERRORS):
                failed_category_count += 1
                last_ordinary_failure = (outcome.plan, failure)
                is_category_unavailable = failure.cause.category == "category_unavailable"
                _safe_emit(
                    runtime_logger,
                    level="WARNING" if is_category_unavailable else "ERROR",
                    event=(
                        "category_unavailable"
                        if is_category_unavailable
                        else "category_collection_failed"
                    ),
                    message=(
                        f"[{task.id}] {outcome.plan.category.display_path} "
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
                        category_run_id=outcome.plan.category_run_id,
                    ),
                    details={
                        "category_id": outcome.plan.category.category_id,
                        "error_category": failure.cause.category,
                        "page_no": failure.failed_page,
                        "status_code": failure.cause.status_code,
                    },
                )
                try:
                    failure_snapshot = database.finish_category_failure(
                        category_run_id=outcome.plan.category_run_id,
                        failed_page=failure.failed_page,
                        error_category=failure.cause.category,
                        finished_at=datetime.now(SHANGHAI_TIMEZONE),
                    )
                    _sync_collection_snapshot(
                        prepared_batch.storage,
                        failure_snapshot,
                    )
                except Exception as error:
                    safe_error = CollectorError(
                        "Could not finalize a failed category run",
                        category="internal_error",
                    )
                    terminal_failure = (
                        outcome.plan,
                        _CategoryAttemptFailed(
                            cause=safe_error,
                            failed_page=failure.failed_page,
                            response_body=None,
                            exception_type=type(error).__name__,
                            category_started=failure.category_started,
                        ),
                        "abandoned",
                    )
            else:
                terminal_failure = (outcome.plan, failure, "abandoned")

            if terminal_failure is not None:
                batch_stop_event.set()
            outcome.should_continue = terminal_failure is None
            outcome.continue_event.set()

    if terminal_failure is not None:
        terminal_plan, terminal_attempt, terminal_status = terminal_failure
        _terminate_batch_and_raise(
            prepared_batch=prepared_batch,
            plan=terminal_plan,
            failure=terminal_attempt,
            status=terminal_status,
            database=database,
            runtime_logger=runtime_logger,
            completed_category_runs=completed_category_runs,
        )

    if not completed_category_runs and last_ordinary_failure is not None:
        # 无任何成功分类时不允许发布空 CSV，按最后一个稳定失败原因收口批次。
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

    if control is not None and control.stop_requested():
        _interrupt_before_next_category(
            prepared_batch=prepared_batch,
            database=database,
            runtime_logger=runtime_logger,
            completed_category_runs=completed_category_runs,
        )
    # finished_at is generated only after every scheduled group has acknowledged completion.
    finished_at = datetime.now(SHANGHAI_TIMEZONE)
    # sorted_category_runs restores the stable external order after concurrent completion.
    sorted_category_runs = tuple(
        sorted(
            completed_category_runs,
            key=lambda item: item.plan.category.discovery_order,
        )
    )
    # saved_item_count is a log-only aggregate over successfully completed categories.
    saved_item_count = sum(len(category_run.entries) for category_run in sorted_category_runs)
    _safe_emit(
        runtime_logger,
        level="INFO",
        event="category_batch_collection_ready",
        message=(
            f"[{task.id}] 分类榜单采集完成：成功 {len(sorted_category_runs)}，"
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
        category_runs=sorted_category_runs,
        failed_category_count=failed_category_count,
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
    """Collect planned categories and stop before publication."""

    if task.id != prepared_batch.task_id:
        raise ValueError("task does not match prepared category batch")
    # 一个一级分类组无需引入跨线程编排，保留原有串行生命周期语义。
    level1_groups = _group_category_plans_by_level1(
        prepared_batch.category_run_plans,
    )
    if len(level1_groups) > 1 and _level1_fetch_workers(client) > 1:
        return _collect_category_batch_by_level1(
            prepared_batch=prepared_batch,
            task=task,
            client=client,
            database=database,
            runtime_logger=runtime_logger,
            control=control,
            level1_groups=level1_groups,
        )
    # 成功列表只接收完整验证并已标记 success 的分类。
    completed_category_runs: list[CollectedCategoryRun] = []
    # 普通失败计数用于部分成功发布汇总，不再因少量异常中止任务。
    failed_category_count = 0
    # last_ordinary_failure 在全部分类失败时提供批次终态原因。
    last_ordinary_failure: tuple[CategoryRunPlan, _CategoryAttemptFailed] | None = None

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
                # 单分类请求或数据契约异常留档后跳过，批次继续采集其他分类。
                failed_category_count += 1
                last_ordinary_failure = (plan, failure)
                is_category_unavailable = failure.cause.category == "category_unavailable"
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
