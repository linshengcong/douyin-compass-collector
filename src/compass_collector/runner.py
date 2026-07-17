"""Dynamic-category login, collection, publication, and status workflows."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from compass_collector.browser import BrowserSession, open_browser
from compass_collector.category_batch import (
    BatchMode as CollectionBatchMode,
    prepare_category_batch,
)
from compass_collector.category_collection import collect_category_batch
from compass_collector.config import AppConfig, TaskConfig
from compass_collector.errors import (
    AuthRequiredError,
    BrowserOperationError,
    CategoryBatchCollectionError,
    CategoryBatchPreparationError,
    CollectionInterruptedError,
    CollectorError,
    PublicationError,
)
from compass_collector.exporter import CsvExporter
from compass_collector.http_client import CompassHttpClient
from compass_collector.models import CollectedCategoryBatch, CollectedCategoryRun
from compass_collector.notifier import (
    BatchMode,
    BatchNotificationSummary,
    BatchSource,
    TaskNotificationResult,
    TaskNotificationStatus,
    deliver_batch_notification,
)
from compass_collector.oss_uploader import OssUploadError, OssUploader
from compass_collector.persistence import (
    BatchCollectionSnapshot,
    Database,
    upgrade_database,
)
from compass_collector.raw_storage import BatchStorage
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


def _safe_emit(runtime_logger: RuntimeLogger, **event_fields: Any) -> None:
    """Keep non-authoritative logging failures from changing persisted results."""

    try:
        runtime_logger.emit(**event_fields)
    except Exception:
        # SQLite、CSV 和 Manifest 终态已经决定后，日志不可用只能降级诊断。
        pass


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
    storage: BatchStorage | None = None,
    category_runs: tuple[CollectedCategoryRun, ...] | None = None,
    csv_path: Path | None = None,
    csv_download_url: str | None = None,
    oss_error_category: str | None = None,
    error_category: str | None = None,
) -> TaskNotificationResult:
    """Build one external-safe task result from config and Manifest counters."""

    # Manifest 只包含已审查计数，不读取原始响应或失败正文。
    manifest = storage.manifest if storage is not None else {}
    # 成功分类存在时，通知页数排除失败分类已经保存的残缺 raw 页。
    successful_page_count = sum(
        len(category_run.raw_pages) for category_run in (category_runs or ())
    )
    # 通知条数只统计可正式发布的完整成功分类商品。
    successful_item_count = sum(
        len(category_run.entries) for category_run in (category_runs or ())
    )
    # 没有分类结果的预采集失败回退到批次安全计数。
    saved_pages = (
        successful_page_count
        if category_runs is not None
        else int(manifest.get("saved_page_count") or 0)
    )
    # category_runs 非空时必须覆盖 Manifest 中失败分类的残缺条数。
    saved_items = (
        successful_item_count
        if category_runs is not None
        else int(manifest.get("collected_item_count") or 0)
    )
    # 对外只发送 CSV 文件名，绝不发送本机目录。
    csv_filename = csv_path.name if csv_path is not None else None
    return TaskNotificationResult(
        task_id=task.id,
        display_name=task.display_name,
        status=status,
        saved_pages=saved_pages,
        saved_items=saved_items,
        csv_filename=csv_filename,
        csv_download_url=csv_download_url,
        oss_error_category=oss_error_category,
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
    client: CompassHttpClient,
    runtime_logger: RuntimeLogger,
    batch_id: str,
    control: CollectionControl | None = None,
    *,
    database: Database,
    mode: CollectionBatchMode,
) -> CollectedCategoryBatch:
    """Discover dynamic categories and collect every ranking before publication."""

    # 阶段二为该顶层任务创建独立批次、分类树 raw 和 pending 分类。
    prepared_batch = prepare_category_batch(
        runtime_root=RUNTIME_ROOT,
        batch_id=batch_id,
        task=plan.task,
        business_date=plan.business_date,
        planned_at=plan.planned_at,
        mode=mode,
        client=client,
        database=database,
        runtime_logger=runtime_logger,
        control=control,
    )
    # 同一个 HTTP 客户端继续串行采集全部动态三级分类分页。
    return collect_category_batch(
        prepared_batch=prepared_batch,
        task=plan.task,
        client=client,
        database=database,
        runtime_logger=runtime_logger,
        control=control,
    )


def _sync_collection_snapshot(storage: BatchStorage, snapshot: object) -> None:
    """Retry one Manifest projection without replaying its SQLite transaction."""

    # 同一权威快照最多重试一次，发布或终止事务不会重复执行。
    for sync_attempt in range(2):
        try:
            storage.sync_collection_snapshot(snapshot)  # type: ignore[arg-type]
            return
        except Exception:
            if sync_attempt == 1:
                raise


def _record_precollection_terminal(
    *,
    plan: TaskExecutionPlan,
    database: Database,
    batch_id: str,
    mode: CollectionBatchMode,
    status: str,
    error_category: str,
    failed_step: str,
    exception_type: str,
    safe_endpoint_path: str,
) -> BatchStorage:
    """Create and finish one task batch before category discovery can start."""

    # 预采集失败仍创建完整 BatchStorage，便于 GUI、日志和 status 关联。
    started_at = datetime.now(SHANGHAI_TIMEZONE)
    storage = BatchStorage(
        runtime_root=RUNTIME_ROOT,
        batch_id=batch_id,
        task_id=plan.task.id,
        business_date=plan.business_date,
        planned_at=plan.planned_at,
        mode=mode,
        started_at=started_at,
    )
    database.create_batch(
        batch_id=batch_id,
        task_id=plan.task.id,
        business_date=plan.business_date,
        planned_at=plan.planned_at,
        mode=mode,
        brand_type=plan.task.filters.brand_type,
        price_bin=plan.task.filters.price_bin,
        manifest_path=storage.manifest_path,
        started_at=started_at,
    )
    # 失败材料只写 runtime，且不包含 Cookie 名称、值或底层异常正文。
    try:
        storage.save_failure(
            status_code=None,
            error_category=error_category,
            response_body=None,
            failed_step=failed_step,
            exception_type=exception_type,
            safe_endpoint_path=safe_endpoint_path,
        )
    except Exception:
        # 诊断材料失败不能阻止 SQLite 批次形成终态。
        pass
    # 分类发现前终止没有任何 category_run，SQLite 先成为权威终态。
    finished_at = datetime.now(SHANGHAI_TIMEZONE)
    database.finish_discovery_failure(
        batch_id=batch_id,
        status=status,
        error_category=error_category,
        finished_at=finished_at,
    )
    # Manifest 从同一 SQLite 快照一次性投影，避免自行拼接计数。
    terminal_snapshot = database.collection_snapshot(batch_id)
    _sync_collection_snapshot(storage, terminal_snapshot)
    return storage


def record_missing_auth(
    plan: TaskExecutionPlan,
    *,
    database: Database,
    batch_id: str,
    mode: CollectionBatchMode,
) -> BatchStorage:
    """Create one auth-required batch when no allowlisted Cookie is available."""

    return _record_precollection_terminal(
        plan=plan,
        database=database,
        batch_id=batch_id,
        mode=mode,
        status="auth_required",
        error_category="auth_required",
        failed_step="read_authentication",
        exception_type="AuthRequiredError",
        safe_endpoint_path=plan.task.rank.endpoint_path,
    )


def record_auth_required_plans(
    plans: list[TaskExecutionPlan],
    *,
    database: Database,
    runtime_logger: RuntimeLogger,
    batch_ids: dict[str, str],
    mode: CollectionBatchMode,
) -> dict[str, BatchStorage]:
    """Record every task blocked by one batch-level authentication failure."""

    # 返回映射让调用方构造每个任务的安全通知计数。
    storages: dict[str, BatchStorage] = {}
    for plan in plans:
        # 每个被阻断顶层任务使用预分配的独立 batch_id。
        task_batch_id = batch_ids[plan.task.id]
        storage = record_missing_auth(
            plan,
            database=database,
            batch_id=task_batch_id,
            mode=mode,
        )
        storages[plan.task.id] = storage
        # auth_required 日志直接关联真实 collection_batch。
        auth_log_context = LogContext(
            batch_id=task_batch_id,
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
    return storages


def record_browser_failure(
    plan: TaskExecutionPlan,
    error: BrowserOperationError,
    *,
    database: Database,
    batch_id: str,
    mode: CollectionBatchMode,
) -> BatchStorage:
    """Persist one browser failure using only the error's safe diagnostic fields."""

    # 先形成 SQLite 与 Manifest 终态，即使截图写入失败也不能留下 running 批次。
    storage = _record_precollection_terminal(
        plan=plan,
        database=database,
        batch_id=batch_id,
        mode=mode,
        status="failed",
        error_category=error.category,
        failed_step=error.failed_step,
        exception_type=error.exception_type,
        safe_endpoint_path=(
            error.safe_page_path or plan.task.rank.endpoint_path
        ),
    )
    try:
        # 浏览器专用诊断覆盖通用 failure.json，并原子保存可用截图。
        storage.save_browser_failure(
            error_category=error.category,
            failed_step=error.failed_step,
            exception_type=error.exception_type,
            safe_page_path=error.safe_page_path,
            page_title=error.page_title,
            screenshot=error.screenshot,
        )
    except Exception:
        # 通用 failure.json 已存在；附加页面材料失败不能覆盖采集终态。
        pass
    return storage


def _collection_mode(*, force: bool, dry_run: bool) -> CollectionBatchMode:
    """Map CLI flags to the SQLite and Manifest batch mode."""

    if dry_run:
        return "dry_run"
    if force:
        return "force"
    return "normal"


def _successful_item_count(collected_batch: CollectedCategoryBatch) -> int:
    """Count only entries belonging to fully successful category runs."""

    return sum(
        len(category_run.entries)
        for category_run in collected_batch.category_runs
    )


def _build_committed_task_result(
    *,
    task: TaskConfig,
    collected_batch: CollectedCategoryBatch,
    snapshot: BatchCollectionSnapshot,
) -> TaskNotificationResult:
    """Build a success result only from an authoritative committed snapshot."""

    if snapshot.status not in {"success", "partial_success"}:
        raise ValueError("committed task result requires a success snapshot")
    # notification_status 与 SQLite 最终状态保持一一对应。
    notification_status = (
        TaskNotificationStatus.PARTIAL_SUCCESS
        if snapshot.status == "partial_success"
        else TaskNotificationStatus.SUCCESS
    )
    # csv_path 只在正式发布快照中存在，dry-run 必须保持为空。
    csv_path = Path(snapshot.csv_path) if snapshot.csv_path is not None else None
    return build_task_notification_result(
        task,
        notification_status,
        storage=collected_batch.storage,
        category_runs=collected_batch.category_runs,
        csv_path=csv_path,
    )


def _finish_unpublished_batch(
    *,
    collected_batch: CollectedCategoryBatch,
    database: Database,
    error: CollectorError,
    exception_type: str,
    status: str,
) -> None:
    """Close one fully collected but unpublished batch after publication stops."""

    try:
        collected_batch.storage.save_failure(
            status_code=error.status_code,
            error_category=error.category,
            response_body=error.response_body,
            failed_step="batch_publication",
            exception_type=exception_type,
            safe_endpoint_path="/runtime/exports",
        )
    except Exception:
        # 发布诊断材料不可写不能留下 running 批次。
        pass
    # 发布事务失败或中止会完整回滚，因此批次仍可从 running 原子收口。
    terminal_snapshot = database.terminate_collection_batch(
        batch_id=collected_batch.batch_id,
        status=status,
        error_category=error.category,
        finished_at=datetime.now(SHANGHAI_TIMEZONE),
        current_category_run_id=None,
        failed_page=None,
    )
    _sync_collection_snapshot(collected_batch.storage, terminal_snapshot)


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
        execution_batch_id=execution_batch_id,
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
    # SQLite/Manifest 使用 normal、force、dry_run 三种内部模式。
    collection_mode = _collection_mode(force=force, dry_run=dry_run)
    # 防止异常分支和正常分支重复发送同一批次。
    notification_sent = False
    # 上传器只在 OSS_ENABLED=true 时做网络调用；未配置时完全不影响采集主链路。
    oss_uploader = OssUploader.from_environment()

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
    # dry-run 也保留 batch/category/raw 审计，因此所有模式都升级并打开数据库。
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
        # 每个顶层 TaskExecutionPlan 预分配独立 collection batch ID。
        task_batch_ids = {
            plan.task.id: uuid4().hex
            for plan in task_plans
        }
        # 手动 run 的 Chrome 在本次命令中统一复用。
        browser_session: BrowserSession | None = None
        # HTTP 客户端可能在登录态检查失败前尚未创建。
        http_client: CompassHttpClient | None = None
        # active_task 精确标记 KeyboardInterrupt 发生时正在处理的顶层任务。
        active_task: TaskConfig | None = None
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
                # 鉴权缺失为每个顶层任务写入独立 auth_required 批次。
                blocked_storages = record_auth_required_plans(
                    task_plans,
                    database=database,
                    runtime_logger=runtime_logger,
                    batch_ids=task_batch_ids,
                    mode=collection_mode,
                )
                for blocked_plan in task_plans:
                    task_results[blocked_plan.task.id] = build_task_notification_result(
                        blocked_plan.task,
                        TaskNotificationStatus.AUTH_REQUIRED,
                        storage=blocked_storages[blocked_plan.task.id],
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
                # 统一 HTTP 节流在 GUI 中可被协作式中止。
                delay_waiter = control.wait_for_delay if control is not None else None
                http_client = CompassHttpClient(
                    config.http,
                    cookies,
                    user_agent,
                    wait_for_delay=delay_waiter,
                )
                # CSV 展示层只在正式发布路径使用。
                csv_exporter = CsvExporter(RUNTIME_ROOT / "exports")
                for plan_index, plan in enumerate(task_plans):
                    # 当前计划在提交后边界中断时不得误标下一个未启动任务。
                    active_task = plan.task
                    # 当前顶层任务所有日志、raw、SQLite 和 Manifest 共用该 ID。
                    task_batch_id = task_batch_ids[plan.task.id]
                    try:
                        collected_batch = collect_task(
                            plan,
                            config,
                            http_client,
                            runtime_logger,
                            task_batch_id,
                            control,
                            database=database,
                            mode=collection_mode,
                        )
                    except (
                        CategoryBatchPreparationError,
                        CategoryBatchCollectionError,
                    ) as task_error:
                        has_failures = True
                        # 采集阶段终止时只统计已完整成功的分类结果。
                        completed_category_runs = (
                            task_error.completed_category_runs
                            if isinstance(task_error, CategoryBatchCollectionError)
                            else ()
                        )
                        if isinstance(task_error.cause, CollectionInterruptedError):
                            task_results[plan.task.id] = build_task_notification_result(
                                plan.task,
                                TaskNotificationStatus.INTERRUPTED,
                                storage=task_error.storage,
                                category_runs=completed_category_runs,
                                error_category="interrupted",
                            )
                            runtime_logger.emit(
                                level="WARNING",
                                event="batch_interrupted",
                                message="已中止本次采集，未发布不完整数据",
                                stage="collection",
                                context=LogContext(
                                    batch_id=task_batch_id,
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
                                category_runs=completed_category_runs,
                                error_category="auth_required",
                            )
                            runtime_logger.emit(
                                level="ERROR",
                                event="authentication_expired",
                                message="登录态失效，本次运行停止后续任务",
                                stage="authentication",
                                context=LogContext(
                                    batch_id=task_batch_id,
                                    task_id=plan.task.id,
                                ),
                                details={"error_category": "auth_required"},
                            )
                            # 后续同批次任务不再请求接口，并写入阻断终态。
                            blocked_plans = task_plans[plan_index + 1 :]
                            blocked_storages = record_auth_required_plans(
                                blocked_plans,
                                database=database,
                                runtime_logger=runtime_logger,
                                batch_ids=task_batch_ids,
                                mode=collection_mode,
                            )
                            for blocked_plan in blocked_plans:
                                task_results[blocked_plan.task.id] = (
                                    build_task_notification_result(
                                        blocked_plan.task,
                                        TaskNotificationStatus.AUTH_REQUIRED,
                                        storage=blocked_storages[
                                            blocked_plan.task.id
                                        ],
                                        error_category="auth_required",
                                    )
                                )
                            break
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.FAILED,
                            storage=task_error.storage,
                            category_runs=completed_category_runs,
                            error_category=task_error.cause.category,
                        )
                        continue
                    if dry_run:
                        try:
                            # dry-run 只终结审计批次，不写商品正式表、CSV 或版本。
                            dry_run_snapshot = database.finalize_dry_run(
                                collected_batch,
                                finished_at=datetime.now(SHANGHAI_TIMEZONE),
                            )
                            # 成功结果赋值必须留在中断保护区内，覆盖提交后的返回边界。
                            task_results[plan.task.id] = _build_committed_task_result(
                                task=plan.task,
                                collected_batch=collected_batch,
                                snapshot=dry_run_snapshot,
                            )
                        except Exception as error:
                            # 发布层未知异常统一转换为稳定安全分类。
                            publication_error = (
                                error
                                if isinstance(error, PublicationError)
                                else PublicationError(
                                    "Unexpected dry-run finalization failure",
                                    category="publication_internal",
                                )
                            )
                            _finish_unpublished_batch(
                                collected_batch=collected_batch,
                                database=database,
                                error=publication_error,
                                exception_type=type(error).__name__,
                                status="failed",
                            )
                            has_failures = True
                            task_results[plan.task.id] = build_task_notification_result(
                                plan.task,
                                TaskNotificationStatus.FAILED,
                                storage=collected_batch.storage,
                                category_runs=collected_batch.category_runs,
                                error_category=publication_error.category,
                            )
                            continue
                        except BaseException as error:
                            # authoritative_snapshot 区分提交前中止和提交后返回边界中止。
                            authoritative_snapshot = database.collection_snapshot(
                                collected_batch.batch_id
                            )
                            if authoritative_snapshot.status in {
                                "success",
                                "partial_success",
                            }:
                                task_results[plan.task.id] = (
                                    _build_committed_task_result(
                                        task=plan.task,
                                        collected_batch=collected_batch,
                                        snapshot=authoritative_snapshot,
                                    )
                                )
                                # 已提交成功只能保留权威结果，禁止反向 terminate。
                                raise
                            # interruption_error 将提交前中止映射为稳定审计终态。
                            interruption_error = CollectionInterruptedError(
                                "Dry-run finalization interrupted",
                                category="interrupted",
                            )
                            _finish_unpublished_batch(
                                collected_batch=collected_batch,
                                database=database,
                                error=interruption_error,
                                exception_type=type(error).__name__,
                                status="interrupted",
                            )
                            task_results[plan.task.id] = (
                                build_task_notification_result(
                                    plan.task,
                                    TaskNotificationStatus.INTERRUPTED,
                                    storage=collected_batch.storage,
                                    category_runs=collected_batch.category_runs,
                                    error_category="interrupted",
                                )
                            )
                            # 外层保留 KeyboardInterrupt/SystemExit 行为并发送汇总通知。
                            raise
                        # dry_run_partial 仅控制成功日志级别和展示文案。
                        dry_run_partial = dry_run_snapshot.status == "partial_success"
                        try:
                            _sync_collection_snapshot(
                                collected_batch.storage,
                                dry_run_snapshot,
                            )
                        except Exception:
                            # SQLite 已形成成功终态，Manifest 差异留给恢复流程处理。
                            _safe_emit(
                                runtime_logger,
                                level="ERROR",
                                event="manifest_sync_failed",
                                message=f"[{plan.task.id}] dry-run Manifest 同步失败",
                                stage="publication",
                                context=LogContext(
                                    batch_id=task_batch_id,
                                    task_id=plan.task.id,
                                ),
                                details={"error_category": "manifest_sync_failed"},
                            )
                        _safe_emit(
                            runtime_logger,
                            level="WARNING" if dry_run_partial else "INFO",
                            event="dry_run_succeeded",
                            message=(
                                f"[{plan.task.id}] dry-run 通过，"
                                f"已校验 {_successful_item_count(collected_batch)} 条，"
                                f"失败分类 {collected_batch.failed_category_count} 个，"
                                "未写入正式商品表/CSV"
                            ),
                            stage="publication",
                            context=LogContext(
                                batch_id=task_batch_id,
                                task_id=plan.task.id,
                            ),
                            details={
                                "dry_run": True,
                                "batch_status": dry_run_snapshot.status,
                                "saved_items": _successful_item_count(
                                    collected_batch
                                ),
                            },
                        )
                        continue
                    try:
                        # CSV 先写完临时文件，正式文件由数据库事务内发布。
                        staged_csv = csv_exporter.prepare(
                            task_id=plan.task.id,
                            display_name=plan.task.display_name,
                            planned_at=plan.planned_at,
                            version=plan.version,
                            batch_id=collected_batch.batch_id,
                            category_runs=collected_batch.category_runs,
                        )
                        # 数据库记录和 CSV 原子替换作为一次协调发布。
                        publication_result = database.publish_collected_batch(
                            collected_batch,
                            version=plan.version,
                            staged_csv=staged_csv,
                            published_at=datetime.now(SHANGHAI_TIMEZONE),
                        )
                        # 成功结果赋值必须留在中断保护区内，覆盖提交后的返回边界。
                        task_results[plan.task.id] = _build_committed_task_result(
                            task=plan.task,
                            collected_batch=collected_batch,
                            snapshot=publication_result.snapshot,
                        )
                    except Exception as error:
                        # CSV 或事务失败不允许留下 running 批次。
                        publication_error = (
                            error
                            if isinstance(error, PublicationError)
                            else PublicationError(
                                "Unexpected collection publication failure",
                                category="publication_internal",
                            )
                        )
                        _finish_unpublished_batch(
                            collected_batch=collected_batch,
                            database=database,
                            error=publication_error,
                            exception_type=type(error).__name__,
                            status="failed",
                        )
                        _safe_emit(
                            runtime_logger,
                            level="ERROR",
                            event="publication_failed",
                            message=(
                                f"[{plan.task.id}] 发布失败，"
                                f"category={publication_error.category}"
                            ),
                            stage="publication",
                            context=LogContext(
                                batch_id=task_batch_id,
                                task_id=plan.task.id,
                            ),
                            details={
                                "error_category": publication_error.category,
                                "artifact_path": str(
                                    collected_batch.storage.artifact_dir
                                ),
                            },
                        )
                        has_failures = True
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.FAILED,
                            storage=collected_batch.storage,
                            category_runs=collected_batch.category_runs,
                            error_category=publication_error.category,
                        )
                        continue
                    except BaseException as error:
                        # authoritative_snapshot 防止提交后返回边界中止被反向收口。
                        authoritative_snapshot = database.collection_snapshot(
                            collected_batch.batch_id
                        )
                        if authoritative_snapshot.status in {
                            "success",
                            "partial_success",
                        }:
                            task_results[plan.task.id] = _build_committed_task_result(
                                task=plan.task,
                                collected_batch=collected_batch,
                                snapshot=authoritative_snapshot,
                            )
                            # SQLite 已正式发布时保留 CSV 和成功通知，禁止 terminate。
                            raise
                        # interruption_error 将进程级中止映射为稳定的本地终态分类。
                        interruption_error = CollectionInterruptedError(
                            "Collection publication interrupted",
                            category="interrupted",
                        )
                        _finish_unpublished_batch(
                            collected_batch=collected_batch,
                            database=database,
                            error=interruption_error,
                            exception_type=type(error).__name__,
                            status="interrupted",
                        )
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.INTERRUPTED,
                            storage=collected_batch.storage,
                            category_runs=collected_batch.category_runs,
                            error_category="interrupted",
                        )
                        # 外层保留 KeyboardInterrupt/SystemExit 行为并统一发送汇总通知。
                        raise
                    # partial_success 只控制正式发布日志级别和文案。
                    partial_success = (
                        publication_result.snapshot.status == "partial_success"
                    )
                    # published_batch 保存正式版本和 CSV 路径供日志展示。
                    published_batch = publication_result.published_batch
                    # SQLite 已正式发布后，Manifest 同步失败不能反向撤销 CSV。
                    try:
                        _sync_collection_snapshot(
                            collected_batch.storage,
                            publication_result.snapshot,
                        )
                    except Exception:
                        _safe_emit(
                            runtime_logger,
                            level="ERROR",
                            event="manifest_sync_failed",
                            message=f"[{plan.task.id}] 发布后 Manifest 同步失败",
                            stage="publication",
                            context=LogContext(
                                batch_id=task_batch_id,
                                task_id=plan.task.id,
                            ),
                            details={"error_category": "manifest_sync_failed"},
                        )
                    _safe_emit(
                        runtime_logger,
                        level="WARNING" if partial_success else "INFO",
                        event="publication_succeeded",
                        message=(
                            f"[{plan.task.id}] 已发布 v{published_batch.version}，"
                            f"失败分类 {collected_batch.failed_category_count} 个，"
                            f"CSV={published_batch.csv_path}"
                        ),
                        stage="publication",
                        context=LogContext(
                            batch_id=task_batch_id,
                            task_id=plan.task.id,
                        ),
                        details={
                            "batch_status": publication_result.snapshot.status,
                            "version": published_batch.version,
                            "csv_path": str(published_batch.csv_path),
                        },
                    )
                    # 正式 CSV 已由 SQLite 协调发布后才允许上传；上传失败不回滚业务发布。
                    try:
                        oss_upload = oss_uploader.upload_csv(
                            csv_path=published_batch.csv_path,
                            business_date=plan.business_date,
                            task_id=plan.task.id,
                            batch_id=published_batch.batch_id,
                        )
                    except OssUploadError as error:
                        task_results[plan.task.id] = build_task_notification_result(
                            plan.task,
                            TaskNotificationStatus.PARTIAL_SUCCESS
                            if partial_success
                            else TaskNotificationStatus.SUCCESS,
                            storage=collected_batch.storage,
                            category_runs=collected_batch.category_runs,
                            csv_path=published_batch.csv_path,
                            oss_error_category=error.category,
                        )
                        _safe_emit(
                            runtime_logger,
                            level="ERROR",
                            event="oss_upload_failed",
                            message=(
                                f"[{plan.task.id}] CSV 已发布，但 OSS 上传失败，"
                                f"category={error.category}"
                            ),
                            stage="oss_upload",
                            context=LogContext(
                                batch_id=task_batch_id,
                                task_id=plan.task.id,
                            ),
                            details={"error_category": error.category},
                        )
                    else:
                        if oss_upload is not None:
                            task_results[plan.task.id] = build_task_notification_result(
                                plan.task,
                                TaskNotificationStatus.PARTIAL_SUCCESS
                                if partial_success
                                else TaskNotificationStatus.SUCCESS,
                                storage=collected_batch.storage,
                                category_runs=collected_batch.category_runs,
                                csv_path=published_batch.csv_path,
                                csv_download_url=oss_upload.download_url,
                            )
                            _safe_emit(
                                runtime_logger,
                                level="INFO",
                                event="oss_upload_succeeded",
                                message=f"[{plan.task.id}] CSV 已上传 OSS",
                                stage="oss_upload",
                                context=LogContext(
                                    batch_id=task_batch_id,
                                    task_id=plan.task.id,
                                ),
                                details={"uploaded": True},
                            )
            # 通知在任务终态形成后立即发送，不等待人工关闭 Chrome。
            send_batch_notification_once()
            if (
                manual
                and config.browser.keep_open_after_manual_run
                and browser_session is not None
            ):
                _safe_emit(
                    runtime_logger,
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
            # 浏览器不可用会阻断全部计划，每个任务仍写独立失败批次。
            for failed_plan in task_plans:
                failed_batch_id = task_batch_ids[failed_plan.task.id]
                storage = record_browser_failure(
                    failed_plan,
                    error,
                    database=database,
                    batch_id=failed_batch_id,
                    mode=collection_mode,
                )
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
                        batch_id=failed_batch_id,
                        task_id=failed_plan.task.id,
                    ),
                    details={
                        "error_category": error.category,
                        "artifact_path": str(storage.artifact_dir),
                    },
                )
            has_failures = True
            send_batch_notification_once()
            if (
                manual
                and config.browser.keep_open_after_manual_run
                and browser_session is not None
            ):
                # 浏览器阶段失败也保留当前页面，便于 GUI 或终端人工检查。
                _safe_emit(
                    runtime_logger,
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
            # interrupted_task 只允许指向真实 active_task，避免误标后续计划。
            interrupted_task: TaskConfig | None = None
            if active_task is not None:
                if (
                    task_results[active_task.id].status
                    is TaskNotificationStatus.NOT_STARTED
                ):
                    interrupted_task = active_task
            else:
                # 浏览器启动前中止时尚无 active_task，回退到首个未开始任务。
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
        # 同一冲突执行使用稳定 ID 串联全部任务和汇总通知。
        busy_batch_id = uuid4().hex
        busy_logger = RuntimeLogger(
            RUNTIME_ROOT / "logs",
            event_sink=control.event_sink if control is not None else None,
            execution_batch_id=busy_batch_id,
        )
        # 冲突任务汇总使用同一 recorded_at，确保消息耗时为零附近。
        busy_recorded_at = datetime.now(SHANGHAI_TIMEZONE)
        # 只有实际写入 skipped_busy 终态的任务进入本次通知。
        busy_results: list[TaskNotificationResult] = []
        try:
            for task in tasks:
                # 每个任务仍拥有独立 collection batch，便于 status 明确展示。
                skipped_batch_id = busy_database.record_skipped_busy_run(
                    task_id=task.id,
                    business_date=planned_at.date(),
                    planned_at=planned_at,
                    recorded_at=busy_recorded_at,
                )
                if skipped_batch_id is None:
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
                        batch_id=skipped_batch_id,
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
    print(
        "planned_at          task_id                         mode     "
        "status           published  version  categories(s/f/total)  batch_id"
    )
    for row in rows:
        # 版本为空表示 dry-run、失败或尚未正式发布。
        version_text = "-" if row.version is None else f"v{row.version}"
        # published_at 非空是正式发布的唯一判据，不能从 status 或 CSV 推断。
        published_text = "yes" if row.published_at is not None else "no"
        # 分类计数按成功、失败、发现总数紧凑展示。
        category_counts = (
            f"{row.successful_category_count}/"
            f"{row.failed_category_count}/"
            f"{row.discovered_category_count}"
        )
        print(
            f"{row.planned_at:%Y-%m-%d %H:%M}  "
            f"{row.task_id:<31} {row.mode:<8} {row.status:<16} "
            f"{published_text:<10} {version_text:<8} "
            f"{category_counts:<22} {row.batch_id}"
        )
    return 0
