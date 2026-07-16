"""Stage-two login, collection, publication, idempotence, and status workflows."""

import random
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from compass_collector.browser import BrowserSession, open_browser
from compass_collector.config import AppConfig, TaskConfig
from compass_collector.errors import (
    AuthRequiredError,
    BrowserOperationError,
    CollectionInterruptedError,
    CollectorError,
    PublicationError,
    ResponseContractError,
    TaskCollectionError,
)
from compass_collector.exporter import CsvExporter
from compass_collector.http_client import ProductRankHttpClient
from compass_collector.models import CollectedTaskRun, ProductRankEntry, RawPageRecord
from compass_collector.notifier import (
    BatchMode,
    BatchNotificationSummary,
    BatchSource,
    TaskNotificationResult,
    TaskNotificationStatus,
    deliver_batch_notification,
)
from compass_collector.persistence import Database, upgrade_database
from compass_collector.product_rank import (
    PaginationPlan,
    build_request_params,
    calculate_pagination_plan,
    parse_page_entries,
    validate_complete_ranking,
    validate_page_payload,
)
from compass_collector.raw_storage import RunStorage
from compass_collector.retention import cleanup_runtime
from compass_collector.run_control import CollectionControl
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy
from compass_collector.runtime_logging import LogContext, RuntimeLogger


# 所有业务日期和计划时间按北京时区固定。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 运行时文件统一位于仓库 runtime 目录下。
RUNTIME_ROOT = Path("runtime")
# Chrome Profile、登录态读取和采集共用同一执行锁。
COLLECTION_LOCK_NAME = "collection.lock"


@dataclass(frozen=True, slots=True)
class TaskExecutionPlan:
    """Bind one task to its business date, planned time, and publication version."""

    task: TaskConfig
    business_date: date
    planned_at: datetime
    version: int


def build_task_notification_result(
    task: TaskConfig,
    status: TaskNotificationStatus,
    *,
    storage: RunStorage | None = None,
    csv_path: Path | None = None,
    error_category: str | None = None,
) -> TaskNotificationResult:
    """Build one external-safe task result from config and Manifest counters."""

    # Manifest 只包含已审查计数，不读取原始响应或失败正文。
    manifest = storage.manifest if storage is not None else {}
    # 对外只发送 CSV 文件名，绝不发送本机目录。
    csv_filename = csv_path.name if csv_path is not None else None
    return TaskNotificationResult(
        task_id=task.id,
        display_name=task.display_name,
        status=status,
        saved_pages=int(manifest.get("saved_pages") or 0),
        saved_items=int(manifest.get("saved_items") or 0),
        csv_filename=csv_filename,
        error_category=error_category,
    )


def select_tasks(config: AppConfig, selected_task_id: str | None) -> list[TaskConfig]:
    """Return enabled tasks or one explicitly selected enabled task."""

    # 启用任务是手动 run 默认的执行集合。
    enabled_tasks = [task for task in config.tasks if task.enabled]
    if selected_task_id is None:
        if not enabled_tasks:
            raise ValueError("no enabled tasks are configured")
        return enabled_tasks
    # 指定任务查找保持 CLI 行为确定。
    matching_tasks = [task for task in enabled_tasks if task.id == selected_task_id]
    if not matching_tasks:
        raise ValueError(f"enabled task not found: {selected_task_id}")
    return matching_tasks


def planned_at_for_task(task: TaskConfig, business_date: date) -> datetime:
    """Map a manual run to the task's same-day fixed cron time."""

    # 当前实时榜单只支持每日固定时分的五段 cron。
    cron_parts = task.schedule.split()
    if len(cron_parts) != 5 or cron_parts[2:] != ["*", "*", "*"]:
        raise ValueError("a daily '<minute> <hour> * * *' schedule is required")
    try:
        # cron 第一和第二段分别是分钟和小时。
        minute = int(cron_parts[0])
        hour = int(cron_parts[1])
    except ValueError as error:
        raise ValueError("schedule minute and hour must be integers") from error
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ValueError("schedule minute or hour is out of range")
    return datetime(
        business_date.year,
        business_date.month,
        business_date.day,
        hour,
        minute,
        tzinfo=SHANGHAI_TIMEZONE,
    )


def prepare_task_plans(
    tasks: list[TaskConfig],
    *,
    database: Database | None,
    force: bool,
    dry_run: bool,
    planned_at_overrides: dict[str, datetime] | None = None,
    skip_any_existing_attempt: bool = False,
) -> list[TaskExecutionPlan]:
    """Apply idempotence before Chrome starts and allocate immutable versions."""

    # Scheduler 可传入真实计划时间；手动命令仍使用当前北京业务日期。
    override_times = planned_at_overrides or {}
    # 手动命令中没有覆盖时间时共用当前北京业务日期。
    current_business_date = datetime.now(SHANGHAI_TIMEZONE).date()
    # 只有真正需要采集的任务才会进入浏览器阶段。
    task_plans: list[TaskExecutionPlan] = []
    for task in tasks:
        # 手动 run 归属到当天配置的计划时间。
        # 调度运行固定使用触发器给出的计划时间，避免延迟后日期漂移。
        planned_at = override_times.get(task.id)
        if planned_at is None:
            planned_at = planned_at_for_task(task, current_business_date)
        # 业务日期永远从计划时间确定，全部分页保持不变。
        business_date = planned_at.astimezone(SHANGHAI_TIMEZONE).date()
        if dry_run:
            task_plans.append(
                TaskExecutionPlan(
                    task=task,
                    business_date=business_date,
                    planned_at=planned_at,
                    version=1,
                )
            )
            continue
        if database is None:
            raise RuntimeError("database is required for an official run")
        if skip_any_existing_attempt and database.has_terminal_run(task.id, planned_at):
            print(
                f"[{task.id}] 计划时间 {planned_at.strftime('%Y-%m-%d %H:%M')} "
                "已有终态记录，Scheduler 跳过"
            )
            continue
        # 已成功快照让默认 run 在打开 Chrome 前跳过。
        successful_batch = database.successful_batch(task.id, planned_at)
        if successful_batch is not None and not force:
            print(
                f"[{task.id}] 计划时间 {planned_at.strftime('%Y-%m-%d %H:%M')} "
                f"已成功发布 v{successful_batch.version}，默认跳过"
            )
            continue
        # 首次发布为 v1，只有 --force 在已有版本上递增。
        version = database.next_version(task.id, planned_at)
        task_plans.append(
            TaskExecutionPlan(
                task=task,
                business_date=business_date,
                planned_at=planned_at,
                version=version,
            )
        )
    return task_plans


def run_login(config: AppConfig) -> int:
    """Open the persistent profile for manual login and close on Enter."""

    # 登录与采集不能同时打开同一个持久化 Chrome Profile。
    login_lock = ProcessLock(RUNTIME_ROOT / "locks" / COLLECTION_LOCK_NAME, "collection")
    with login_lock:
        # 登录命令仅管理 Chrome，不创建 HTTP 客户端或数据库。
        browser_session = open_browser(config.browser)
        try:
            browser_session.wait_for_manual_exit(
                "Chrome 已打开。完成登录和检查后，按 Enter 关闭浏览器\n"
            )
        finally:
            browser_session.close()
    return 0


def collect_task(
    plan: TaskExecutionPlan,
    config: AppConfig,
    client: ProductRankHttpClient,
    runtime_logger: RuntimeLogger,
    batch_id: str,
    control: CollectionControl | None = None,
) -> CollectedTaskRun:
    """Collect, parse, and fully validate one task without publishing it."""

    # 开始时间在创建 Manifest 前记录，供数据库 run 使用。
    started_at = datetime.now(SHANGHAI_TIMEZONE)
    # 每个任务尝试拥有独立的 run_id 和原始数据目录。
    storage = RunStorage(
        runtime_root=RUNTIME_ROOT,
        task_id=plan.task.id,
        business_date=plan.business_date,
        max_items=plan.task.pagination.max_items,
    )
    # 任务日志上下文从此处开始同时具备 batch_id、run_id 和 task_id。
    log_context = LogContext(
        batch_id=batch_id,
        run_id=storage.run_id,
        task_id=plan.task.id,
    )
    runtime_logger.emit(
        level="INFO",
        event="task_started",
        message=f"[{plan.task.id}] 开始采集",
        stage="collection",
        context=log_context,
        details={"planned_at": plan.planned_at.isoformat()},
    )
    # 首页成功后固定接口 total，后续分页不允许变化。
    expected_total: int | None = None
    # 首页返回 total 之前无法确定目标页数。
    pagination_plan: PaginationPlan | None = None
    # 当前请求从第 1 页开始串行递增。
    page_no = 1
    # 进度计数只包含已校验、解析并原子发布的分页。
    saved_pages = 0
    saved_items = 0
    # 当前响应仅在契约失败时用于本地留档。
    current_response_body = b""
    current_status_code: int | None = None
    # 整榜商品与原始页索引在全部分页成功前仅存在内存。
    entries: list[ProductRankEntry] = []
    raw_pages: list[RawPageRecord] = []
    try:
        while pagination_plan is None or page_no <= pagination_plan.target_pages:
            if control is not None and control.stop_requested():
                raise CollectionInterruptedError(
                    "Collection interrupted by developer",
                    category="interrupted",
                )
            # 请求参数只包含已确认的业务字段。
            params = build_request_params(plan.task, plan.business_date, page_no)
            # 当前页不做任何自动重试。
            page_response = client.get_page(plan.task, params)
            if control is not None and control.stop_requested():
                raise CollectionInterruptedError(
                    "Collection interrupted after current request",
                    category="interrupted",
                )
            current_response_body = page_response.body
            current_status_code = page_response.status_code
            # 响应在写入 gzip 前完成分页契约校验。
            page_contract = validate_page_payload(
                page_response.payload,
                requested_page=page_no,
                expected_total=expected_total,
            )
            if expected_total is None:
                expected_total = page_contract.total
                pagination_plan = calculate_pagination_plan(
                    total=expected_total,
                    max_items=plan.task.pagination.max_items,
                )
            # 每页捕获时间在 HTTP 响应通过页级契约后记录。
            captured_at = datetime.now(SHANGHAI_TIMEZONE)
            # 原始响应先于商品解析落盘，解析失败时仍可排查现场。
            page_path = storage.write_page(page_no, page_response.payload)
            # 商品字段解析不对原始数值做展示换算。
            page_entries = parse_page_entries(
                page_response.payload,
                page_no=page_no,
                captured_at=captured_at,
            )
            entries.extend(page_entries)
            raw_pages.append(
                RawPageRecord(
                    page_no=page_no,
                    path=page_path,
                    item_count=page_contract.item_count,
                    captured_at=captured_at,
                )
            )
            saved_pages += 1
            saved_items += page_contract.item_count
            storage.update_progress(
                api_total=expected_total,
                target_items=pagination_plan.target_items,
                saved_pages=saved_pages,
                saved_items=saved_items,
            )
            runtime_logger.emit(
                level="INFO",
                event="page_collected",
                message=(
                    f"[{plan.task.id}] 已保存并解析第 "
                    f"{page_no}/{pagination_plan.target_pages} 页，"
                    f"累计 {saved_items}/{pagination_plan.target_items} 条"
                ),
                stage="collection",
                context=log_context,
                details={
                    "page_no": page_no,
                    "target_pages": pagination_plan.target_pages,
                    "saved_items": saved_items,
                    "target_items": pagination_plan.target_items,
                },
            )
            if page_no >= pagination_plan.target_pages:
                break
            # 正常分页请求之间使用配置范围内的随机间隔。
            delay_seconds = random.uniform(
                config.http.page_interval_seconds.min,
                config.http.page_interval_seconds.max,
            )
            runtime_logger.emit(
                level="INFO",
                event="page_interval",
                message=f"[{plan.task.id}] 等待 {delay_seconds:.2f} 秒后请求下一页",
                stage="rate_control",
                context=log_context,
                details={"delay_seconds": round(delay_seconds, 2)},
            )
            if control is None:
                time.sleep(delay_seconds)
            elif control.wait_for_delay(delay_seconds):
                raise CollectionInterruptedError(
                    "Collection interrupted during page interval",
                    category="interrupted",
                )
            page_no += 1
        if pagination_plan is None or saved_items != pagination_plan.target_items:
            raise ResponseContractError(
                "saved item count does not equal target items",
                category="incomplete_collection",
            )
        validate_complete_ranking(entries, target_items=pagination_plan.target_items)
        runtime_logger.emit(
            level="INFO",
            event="ranking_validated",
            message=f"[{plan.task.id}] 整榜校验通过，共 {len(entries)} 条",
            stage="validation",
            context=log_context,
            details={"target_items": pagination_plan.target_items},
        )
        # 完成时间在整榜契约通过后记录。
        finished_at = datetime.now(SHANGHAI_TIMEZONE)
        return CollectedTaskRun(
            task_id=plan.task.id,
            business_date=plan.business_date,
            started_at=started_at,
            finished_at=finished_at,
            storage=storage,
            entries=tuple(entries),
            raw_pages=tuple(raw_pages),
        )
    except (KeyboardInterrupt, CollectionInterruptedError) as error:
        storage.mark_interrupted(failed_page=page_no)
        runtime_logger.emit(
            level="WARNING",
            event="task_interrupted",
            message=f"[{plan.task.id}] 采集被人工中断",
            stage="collection",
            context=log_context,
            details={"page_no": page_no, "error_category": "interrupted"},
        )
        # Ctrl-C 与 GUI 中止统一转换为可汇总的 interrupted 任务终态。
        interrupted_error = (
            error
            if isinstance(error, CollectionInterruptedError)
            else CollectionInterruptedError(
                "Collection interrupted by developer",
                category="interrupted",
            )
        )
        raise TaskCollectionError(interrupted_error, storage) from error
    except CollectorError as error:
        # HTTP 错误自带当前 body，契约错误使用已解析的当前响应。
        failure_body = error.response_body
        if failure_body is None and isinstance(error, ResponseContractError):
            failure_body = current_response_body
        # HTTP 错误自带状态码，契约错误使用当前 2xx 状态。
        failure_status_code = error.status_code
        if failure_status_code is None and isinstance(error, ResponseContractError):
            failure_status_code = current_status_code
        storage.save_failure_response(
            status_code=failure_status_code,
            error_category=error.category,
            response_body=failure_body,
            failed_step="http_request_or_contract_validation",
            exception_type=type(error).__name__,
            safe_endpoint_path=plan.task.rank.endpoint_path,
        )
        if isinstance(error, AuthRequiredError):
            # 鉴权失败是独立终态，Scheduler 据此阻断同批次后续任务。
            storage.mark_auth_required(failed_page=page_no)
        else:
            storage.mark_failed(failed_page=page_no, error_category=error.category)
        runtime_logger.emit(
            level="ERROR",
            event="task_collection_failed",
            message=f"[{plan.task.id}] 采集失败，category={error.category}",
            stage="collection",
            context=log_context,
            details={
                "page_no": page_no,
                "status_code": failure_status_code,
                "error_category": error.category,
                "artifact_path": str(storage.artifact_dir),
            },
        )
        raise TaskCollectionError(error, storage) from error
    except Exception as error:
        # 未预期错误仅落稳定分类和异常类型，不保存异常文本。
        safe_error = CollectorError(
            "Unexpected collection failure",
            category="internal_error",
        )
        storage.save_runtime_failure(
            error_category=safe_error.category,
            failed_step="collection_internal",
            exception_type=type(error).__name__,
        )
        storage.mark_failed(failed_page=page_no, error_category=safe_error.category)
        runtime_logger.emit(
            level="ERROR",
            event="task_internal_failed",
            message=f"[{plan.task.id}] 采集内部失败，category=internal_error",
            stage="collection",
            context=log_context,
            details={
                "page_no": page_no,
                "error_category": safe_error.category,
                "artifact_path": str(storage.artifact_dir),
            },
        )
        raise TaskCollectionError(safe_error, storage) from error


def record_missing_auth(task: TaskConfig, business_date: date) -> RunStorage:
    """Create a failed run manifest when no allowlisted Cookie is available."""

    # 失败 Manifest 不包含缺失的 Cookie 名称。
    storage = RunStorage(
        runtime_root=RUNTIME_ROOT,
        task_id=task.id,
        business_date=business_date,
        max_items=task.pagination.max_items,
    )
    storage.save_runtime_failure(
        error_category="auth_required",
        failed_step="read_authentication",
        exception_type="AuthRequiredError",
    )
    storage.mark_auth_required(failed_page=1)
    return storage


def record_auth_required_plans(
    plans: list[TaskExecutionPlan],
    *,
    database: Database | None,
    runtime_logger: RuntimeLogger,
    execution_batch_id: str,
) -> None:
    """Record every task blocked by one batch-level authentication failure."""

    for plan in plans:
        # 每个被阻断任务拥有独立 run_id，status 可完整展示批次影响范围。
        storage = record_missing_auth(plan.task, plan.business_date)
        if database is not None:
            database.record_failed_run(
                storage,
                planned_at=plan.planned_at,
                error_category="auth_required",
            )
        # auth_required 任务日志使用完整批次、运行和任务上下文。
        auth_log_context = LogContext(
            batch_id=execution_batch_id,
            run_id=storage.run_id,
            task_id=plan.task.id,
        )
        runtime_logger.emit(
            level="ERROR",
            event="authentication_required",
            message=(
                f"[{plan.task.id}] 未找到可用的白名单认证状态，"
                "本批次任务已阻断"
            ),
            stage="authentication",
            context=auth_log_context,
            details={
                "error_category": "auth_required",
                "artifact_path": str(storage.artifact_dir),
            },
        )


def record_browser_failure(
    plan: TaskExecutionPlan,
    error: BrowserOperationError,
) -> RunStorage:
    """Persist one browser failure using only the error's safe diagnostic fields."""

    # 浏览器失败也创建独立 run_id，便于数据库、日志与材料互相定位。
    storage = RunStorage(
        runtime_root=RUNTIME_ROOT,
        task_id=plan.task.id,
        business_date=plan.business_date,
        max_items=plan.task.pagination.max_items,
    )
    storage.save_browser_failure(
        error_category=error.category,
        failed_step=error.failed_step,
        exception_type=error.exception_type,
        safe_page_path=error.safe_page_path,
        page_title=error.page_title,
        screenshot=error.screenshot,
    )
    storage.mark_failed(failed_page=1, error_category=error.category)
    return storage


def _run_collection_unlocked(
    config: AppConfig,
    selected_task_id: str | None,
    *,
    force: bool,
    dry_run: bool,
    manual: bool = True,
    scheduled_tasks: list[TaskConfig] | None = None,
    planned_at_overrides: dict[str, datetime] | None = None,
    control: CollectionControl | None = None,
    run_source: BatchSource = BatchSource.TERMINAL,
) -> int:
    """Run selected tasks and optionally publish SQLite plus CSV snapshots."""

    # 任务选择在任何数据库或浏览器操作前完成。
    selected_tasks = (
        scheduled_tasks
        if scheduled_tasks is not None
        else select_tasks(config, selected_task_id)
    )
    if not selected_tasks:
        raise ValueError("no scheduled tasks were provided")
    # 批次开始时间早于清理和规划，用于通知展示完整命令耗时。
    batch_started_at = datetime.now(SHANGHAI_TIMEZONE)
    # 每次 CLI run 使用独立批次 ID 串联 JSONL 日志。
    execution_batch_id = uuid4().hex
    # JSONL 日志按北京时间自然日自动选择文件。
    runtime_logger = RuntimeLogger(
        RUNTIME_ROOT / "logs",
        event_sink=control.event_sink if control is not None else None,
    )
    # 每个选中任务先标记 not_started，后续分支只覆盖真实结果。
    task_results = {
        task.id: build_task_notification_result(
            task,
            TaskNotificationStatus.NOT_STARTED,
        )
        for task in selected_tasks
    }
    # 当前命令模式在整个批次内保持不变。
    batch_mode = (
        BatchMode.DRY_RUN
        if dry_run
        else BatchMode.FORCE
        if force
        else BatchMode.OFFICIAL
    )
    # 防止异常分支和正常分支重复发送同一批次。
    notification_sent = False

    def send_batch_notification_once() -> None:
        """Deliver one ordered batch summary without changing collection state."""

        nonlocal notification_sent
        if notification_sent:
            return
        # selected_tasks 顺序是汇总消息的稳定任务顺序。
        summary = BatchNotificationSummary(
            batch_id=execution_batch_id,
            source=run_source,
            mode=batch_mode,
            started_at=batch_started_at,
            finished_at=datetime.now(SHANGHAI_TIMEZONE),
            tasks=tuple(task_results[task.id] for task in selected_tasks),
        )
        deliver_batch_notification(summary, runtime_logger)
        notification_sent = True
    # 保留清理在新运行材料创建前执行，且不触碰数据库、CSV 和 Profile。
    cleanup_summary = cleanup_runtime(RUNTIME_ROOT, config.retention)
    runtime_logger.emit(
        level="WARNING" if cleanup_summary.failures else "INFO",
        event="retention_cleanup_finished",
        message=(
            "运行时保留清理完成"
            if not cleanup_summary.failures
            else "运行时保留清理完成，但有项目删除失败"
        ),
        stage="retention",
        details={"cleanup_counts": cleanup_summary.as_log_details()},
    )
    # dry-run 不读写正式数据库。
    database: Database | None = None
    if not dry_run:
        upgrade_database(config.database.path)
        database = Database(config.database.path)
    try:
        # 幂等检查和版本分配在打开 Chrome 前完成。
        task_plans = prepare_task_plans(
            selected_tasks,
            database=database,
            force=force,
            dry_run=dry_run,
            planned_at_overrides=planned_at_overrides,
            skip_any_existing_attempt=not manual,
        )
        # 未进入计划的人工任务因已有成功终态而幂等跳过。
        planned_task_ids = {plan.task.id for plan in task_plans}
        for selected_task in selected_tasks:
            if selected_task.id not in planned_task_ids:
                task_results[selected_task.id] = build_task_notification_result(
                    selected_task,
                    TaskNotificationStatus.SKIPPED,
                )
        if not task_plans:
            runtime_logger.emit(
                level="INFO",
                event="batch_skipped",
                message="没有需要采集的任务",
                stage="planning",
            )
            # 人工显式命令的幂等跳过也发送一次 skipped 汇总。
            if manual:
                send_batch_notification_once()
            return 0
        # 手动 run 的 Chrome 在本次命令中统一复用。
        browser_session: BrowserSession | None = None
        # HTTP 客户端可能在登录态检查失败前尚未创建。
        http_client: ProductRankHttpClient | None = None
        # 任何任务失败都让 CLI 返回非零状态。
        has_failures = False
        try:
            browser_session = open_browser(config.browser)
            # 运行时仅读取白名单内且对目标 API 适用的 Cookie。
            cookies = browser_session.whitelisted_cookies(config.auth.cookie_names)
            runtime_logger.emit(
                level="INFO",
                event="authentication_loaded",
                message=f"已从当前 Profile 读取 {len(cookies)} 项白名单认证状态",
                stage="authentication",
                details={"authentication_item_count": len(cookies)},
            )
            if not cookies:
                # 鉴权缺失阻断整个手动批次。
                record_auth_required_plans(
                    task_plans,
                    database=database,
                    runtime_logger=runtime_logger,
                    execution_batch_id=execution_batch_id,
                )
                for blocked_plan in task_plans:
                    task_results[blocked_plan.task.id] = build_task_notification_result(
                        blocked_plan.task,
                        TaskNotificationStatus.AUTH_REQUIRED,
                        error_category="auth_required",
                    )
                runtime_logger.emit(
                    level="ERROR",
                    event="authentication_batch_blocked",
                    message="未找到可用的白名单认证状态，请先在当前 Chrome 中登录",
                    stage="authentication",
                )
                has_failures = True
            else:
                # User-Agent 从当前正式版 Chrome 动态读取。
                user_agent = browser_session.user_agent()
                http_client = ProductRankHttpClient(config.http, cookies, user_agent)
                # CSV 展示层只在正式发布路径使用。
                csv_exporter = CsvExporter(RUNTIME_ROOT / "exports")
                for plan_index, plan in enumerate(task_plans):
                    try:
                        collected_run = collect_task(
                            plan,
                            config,
                            http_client,
                            runtime_logger,
                            execution_batch_id,
                            control,
                        )
                    except TaskCollectionError as task_error:
                        has_failures = True
                        if database is not None:
                            database.record_failed_run(
                                task_error.storage,
                                planned_at=plan.planned_at,
                                error_category=task_error.cause.category,
                            )
                        if isinstance(task_error.cause, CollectionInterruptedError):
                            task_results[plan.task.id] = build_task_notification_result(
                                plan.task,
                                TaskNotificationStatus.INTERRUPTED,
                                storage=task_error.storage,
                                error_category="interrupted",
                            )
                            runtime_logger.emit(
                                level="WARNING",
                                event="batch_interrupted",
                                message="已中止本次采集，未发布不完整数据",
                                stage="collection",
                                context=LogContext(
                                    batch_id=execution_batch_id,
                                    run_id=task_error.storage.run_id,
                                    task_id=plan.task.id,
                                ),
                                details={"error_category": "interrupted"},
                            )
                            break
                        if isinstance(task_error.cause, AuthRequiredError):
                            task_results[plan.task.id] = build_task_notification_result(
                                plan.task,
                                TaskNotificationStatus.AUTH_REQUIRED,
                                storage=task_error.storage,
                                error_category="auth_required",
                            )
                            runtime_logger.emit(
                                level="ERROR",
                                event="authentication_expired",
                                message="登录态失效，本次手动运行停止后续任务",
                                stage="authentication",
                                context=LogContext(
                                    batch_id=execution_batch_id,
                                    run_id=task_error.storage.run_id,
                                    task_id=plan.task.id,
                                ),
                                details={"error_category": "auth_required"},
                            )
                            # 后续同批次任务不再请求接口，并写入阻断终态。
                            blocked_plans = task_plans[plan_index + 1 :]
                            record_auth_required_plans(
                                blocked_plans,
                                database=database,
                                runtime_logger=runtime_logger,
                                execution_batch_id=execution_batch_id,
                            )
                            for blocked_plan in blocked_plans:
                                task_results[blocked_plan.task.id] = (
                                    build_task_notification_result(
                                        blocked_plan.task,
                                        TaskNotificationStatus.AUTH_REQUIRED,
                                        error_category="auth_required",
                                    )
                                )
                            break
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.FAILED,
                            storage=task_error.storage,
                            error_category=task_error.cause.category,
                        )
                        continue
                    if dry_run:
                        collected_run.storage.mark_success()
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.SUCCESS,
                            storage=collected_run.storage,
                        )
                        runtime_logger.emit(
                            level="INFO",
                            event="dry_run_succeeded",
                            message=(
                                f"[{plan.task.id}] dry-run 通过，"
                                f"已校验 {len(collected_run.entries)} 条，未写入 SQLite/CSV"
                            ),
                            stage="publication",
                            context=LogContext(
                                batch_id=execution_batch_id,
                                run_id=collected_run.storage.run_id,
                                task_id=plan.task.id,
                            ),
                            details={
                                "dry_run": True,
                                "target_items": len(collected_run.entries),
                            },
                        )
                        continue
                    if database is None:
                        raise RuntimeError("database is required for publication")
                    try:
                        # CSV 先写完临时文件，正式文件由数据库事务内发布。
                        staged_csv = csv_exporter.prepare(
                            task_id=plan.task.id,
                            planned_at=plan.planned_at,
                            version=plan.version,
                            run_id=collected_run.storage.run_id,
                            entries=collected_run.entries,
                        )
                        # 数据库记录和 CSV 原子替换作为一次协调发布。
                        published_batch = database.publish_snapshot(
                            collected_run,
                            planned_at=plan.planned_at,
                            version=plan.version,
                            staged_csv=staged_csv,
                        )
                    except PublicationError as error:
                        collected_run.storage.mark_failed(
                            failed_page=len(collected_run.raw_pages),
                            error_category=error.category,
                        )
                        database.record_failed_run(
                            collected_run.storage,
                            planned_at=plan.planned_at,
                            error_category=error.category,
                        )
                        runtime_logger.emit(
                            level="ERROR",
                            event="publication_failed",
                            message=f"[{plan.task.id}] 发布失败，category={error.category}",
                            stage="publication",
                            context=LogContext(
                                batch_id=execution_batch_id,
                                run_id=collected_run.storage.run_id,
                                task_id=plan.task.id,
                            ),
                            details={
                                "error_category": error.category,
                                "artifact_path": str(collected_run.storage.artifact_dir),
                            },
                        )
                        has_failures = True
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.FAILED,
                            storage=collected_run.storage,
                            error_category=error.category,
                        )
                        continue
                    collected_run.storage.mark_success()
                    task_results[plan.task.id] = build_task_notification_result(
                        plan.task,
                        TaskNotificationStatus.SUCCESS,
                        storage=collected_run.storage,
                        csv_path=published_batch.csv_path,
                    )
                    runtime_logger.emit(
                        level="INFO",
                        event="publication_succeeded",
                        message=(
                            f"[{plan.task.id}] 已发布 v{published_batch.version}，"
                            f"CSV={published_batch.csv_path}"
                        ),
                        stage="publication",
                        context=LogContext(
                            batch_id=execution_batch_id,
                            run_id=collected_run.storage.run_id,
                            task_id=plan.task.id,
                        ),
                        details={
                            "version": published_batch.version,
                            "csv_path": str(published_batch.csv_path),
                        },
                    )
            # 通知在任务终态形成后立即发送，不等待人工关闭 Chrome。
            send_batch_notification_once()
            if (
                manual
                and config.browser.keep_open_after_manual_run
                and browser_session is not None
            ):
                runtime_logger.emit(
                    level="WARNING" if has_failures else "INFO",
                    event="manual_inspection_ready",
                    message="采集流程已结束，Chrome 已保留供调试检查",
                    stage="manual_debug",
                )
                if control is not None and control.keep_browser_open:
                    control.wait_for_browser_close()
                else:
                    browser_session.wait_for_manual_exit(
                        "采集流程已结束。完成调试检查后，按 Enter 关闭浏览器\n"
                    )
        except BrowserOperationError as error:
            # 浏览器错误只记录内部步骤和异常类型，不输出底层异常文本。
            failed_plan = task_plans[0]
            storage = record_browser_failure(failed_plan, error)
            if database is not None:
                database.record_failed_run(
                    storage,
                    planned_at=failed_plan.planned_at,
                    error_category=error.category,
                )
            has_failures = True
            task_results[failed_plan.task.id] = build_task_notification_result(
                failed_plan.task,
                TaskNotificationStatus.FAILED,
                storage=storage,
                error_category=error.category,
            )
            runtime_logger.emit(
                level="ERROR",
                event="browser_operation_failed",
                message=(
                    f"[{failed_plan.task.id}] 浏览器操作失败，"
                    f"category={error.category}"
                ),
                stage="browser",
                context=LogContext(
                    batch_id=execution_batch_id,
                    run_id=storage.run_id,
                    task_id=failed_plan.task.id,
                ),
                details={
                    "error_category": error.category,
                    "artifact_path": str(storage.artifact_dir),
                },
            )
            send_batch_notification_once()
            if (
                manual
                and config.browser.keep_open_after_manual_run
                and browser_session is not None
            ):
                # 浏览器阶段失败也保留当前页面，便于 GUI 或终端人工检查。
                runtime_logger.emit(
                    level="WARNING",
                    event="manual_inspection_ready",
                    message="采集流程已结束，Chrome 已保留供调试检查",
                    stage="manual_debug",
                )
                if control is not None and control.keep_browser_open:
                    control.wait_for_browser_close()
                else:
                    browser_session.wait_for_manual_exit(
                        "采集流程已结束。完成调试检查后，按 Enter 关闭浏览器\n"
                    )
        except KeyboardInterrupt:
            has_failures = True
            # 如果 Ctrl-C 发生在任务创建 Manifest 之前，仍为首个未开始任务记录中止。
            interrupted_task = next(
                (
                    task
                    for task in selected_tasks
                    if task_results[task.id].status
                    is TaskNotificationStatus.NOT_STARTED
                ),
                None,
            )
            if interrupted_task is not None:
                task_results[interrupted_task.id] = build_task_notification_result(
                    interrupted_task,
                    TaskNotificationStatus.INTERRUPTED,
                    error_category="interrupted",
                )
            runtime_logger.emit(
                level="WARNING",
                event="batch_interrupted",
                message="已中断手动运行",
                stage="manual_debug",
            )
            send_batch_notification_once()
        finally:
            if http_client is not None:
                http_client.close()
            if browser_session is not None:
                browser_session.close()
        return 1 if has_failures else 0
    finally:
        if database is not None:
            database.close()


def run_collection(
    config: AppConfig,
    selected_task_id: str | None,
    *,
    force: bool,
    dry_run: bool,
    manual: bool = True,
    scheduled_tasks: list[TaskConfig] | None = None,
    planned_at_overrides: dict[str, datetime] | None = None,
    control: CollectionControl | None = None,
    run_source: BatchSource = BatchSource.TERMINAL,
) -> int:
    """Run one mutually exclusive Chrome-backed collection operation."""

    # 执行锁覆盖采集和手动 Chrome 检查期，避免 Profile 被第二个进程打开。
    collection_lock = ProcessLock(
        RUNTIME_ROOT / "locks" / COLLECTION_LOCK_NAME,
        "collection",
    )
    with collection_lock:
        return _run_collection_unlocked(
            config,
            selected_task_id,
            force=force,
            dry_run=dry_run,
            manual=manual,
            scheduled_tasks=scheduled_tasks,
            planned_at_overrides=planned_at_overrides,
            control=control,
            run_source=run_source,
        )


def run_scheduled_collection(
    config: AppConfig,
    tasks: list[TaskConfig],
    planned_at: datetime,
    control: CollectionControl | None = None,
) -> int:
    """Execute one due task group without waiting for keyboard input."""

    # 同一计划时刻的任务共享覆盖时间，并在一个 Chrome 批次内串行执行。
    planned_at_overrides = {task.id: planned_at for task in tasks}
    try:
        return run_collection(
            config,
            selected_task_id=None,
            force=False,
            dry_run=False,
            manual=False,
            scheduled_tasks=tasks,
            planned_at_overrides=planned_at_overrides,
            control=control,
            run_source=BatchSource.SCHEDULER,
        )
    except RuntimeLockBusy:
        # 手动调试占用 Chrome 时，本次计划只记终态，不排队或自动重试。
        upgrade_database(config.database.path)
        busy_database = Database(config.database.path)
        busy_logger = RuntimeLogger(
            RUNTIME_ROOT / "logs",
            event_sink=control.event_sink if control is not None else None,
        )
        # 同一冲突批次使用稳定 batch_id 串联所有任务。
        busy_batch_id = uuid4().hex
        # 冲突任务汇总使用同一 recorded_at，确保消息耗时为零附近。
        busy_recorded_at = datetime.now(SHANGHAI_TIMEZONE)
        # 只有实际写入 skipped_busy 终态的任务进入本次通知。
        busy_results: list[TaskNotificationResult] = []
        try:
            for task in tasks:
                # 每个任务仍拥有独立 run_id，便于 status 明确显示 skipped_busy。
                run_id = busy_database.record_skipped_busy_run(
                    task_id=task.id,
                    business_date=planned_at.date(),
                    planned_at=planned_at,
                    recorded_at=busy_recorded_at,
                )
                if run_id is None:
                    continue
                busy_results.append(
                    build_task_notification_result(
                        task,
                        TaskNotificationStatus.SKIPPED_BUSY,
                        error_category="skipped_busy",
                    )
                )
                busy_logger.emit(
                    level="WARNING",
                    event="scheduled_task_skipped_busy",
                    message=(
                        f"[{task.id}] Chrome 正被其他任务使用，"
                        "本次定时采集已跳过"
                    ),
                    stage="scheduling",
                    context=LogContext(
                        batch_id=busy_batch_id,
                        run_id=run_id,
                        task_id=task.id,
                    ),
                    details={
                        "planned_at": planned_at.isoformat(),
                        "error_category": "skipped_busy",
                    },
                )
        finally:
            busy_database.close()
        if busy_results:
            deliver_batch_notification(
                BatchNotificationSummary(
                    batch_id=busy_batch_id,
                    source=BatchSource.SCHEDULER,
                    mode=BatchMode.OFFICIAL,
                    started_at=busy_recorded_at,
                    finished_at=datetime.now(SHANGHAI_TIMEZONE),
                    tasks=tuple(busy_results),
                ),
                busy_logger,
            )
        return 0


def run_status(config: AppConfig, limit: int) -> int:
    """Upgrade the database and print recent task-attempt status rows."""

    upgrade_database(config.database.path)
    # status 查询使用独立短生命周期数据库对象。
    database = Database(config.database.path)
    try:
        # 最近 run 列表同时包含成功发布和失败尝试。
        rows = database.recent_status(limit=limit)
    finally:
        database.close()
    if not rows:
        print("暂无运行记录")
        return 0
    print("planned_at          task_id                         status       version  run_id")
    for row in rows:
        # 无正式批次的失败 run 使用短横线表示无版本。
        version_text = "-" if row.version is None else f"v{row.version}"
        print(
            f"{row.planned_at:%Y-%m-%d %H:%M}  "
            f"{row.task_id:<31} {row.status:<12} {version_text:<8} {row.run_id}"
        )
    return 0
