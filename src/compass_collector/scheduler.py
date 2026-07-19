"""Durable APScheduler orchestration for daily Beijing-time task groups."""

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
import signal
import time
from threading import Event, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from compass_collector.config import AppConfig, TaskConfig
from compass_collector.notifier import (
    BatchMode,
    BatchNotificationSummary,
    BatchSource,
    TaskNotificationResult,
    TaskNotificationStatus,
    deliver_batch_notification,
)
from compass_collector.persistence import Database, upgrade_database
from compass_collector.runner import planned_at_for_task, run_scheduled_collection
from compass_collector.run_control import CollectionControl
from compass_collector.runtime_locks import ProcessLock
from compass_collector.runtime_logging import LogContext, RuntimeLogger
from compass_collector.app_paths import runtime_root


# Scheduler 与采集业务日期统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 调度日志在开发时沿用 runtime，便携版使用应用包内持久目录。
RUNTIME_ROOT = runtime_root()
# Scheduler 实例锁与实际采集执行锁相互独立。
SCHEDULER_LOCK_NAME = "scheduler.lock"
# 运行回调签名允许测试替换真实浏览器采集。
ScheduledRunCallback = Callable[[AppConfig, list[TaskConfig], datetime], int]


def aware_checkpoint(value: datetime) -> datetime:
    """Restore SQLite wall-clock checkpoints to Beijing-aware datetimes."""

    return value.replace(tzinfo=SHANGHAI_TIMEZONE)


def enumerate_fire_times(
    task: TaskConfig,
    checkpoint: datetime,
    now: datetime,
) -> list[datetime]:
    """Enumerate every configured fire strictly after a durable checkpoint."""

    # CronTrigger 负责解析已由 Pydantic 限定的五段每日 cron。
    trigger = CronTrigger.from_crontab(task.schedule, timezone=SHANGHAI_TIMEZONE)
    # 从检查点后一微秒开始，避免重复返回恰好等于检查点的触发时刻。
    cursor = checkpoint + timedelta(microseconds=1)
    # APScheduler 需要前一次触发时间推进重复计算。
    previous_fire_time: datetime | None = None
    # 结果按时间顺序返回，跨多日休眠时逐日处理。
    fire_times: list[datetime] = []
    while True:
        # 下一次时间由固定 cron 和北京时间共同决定。
        fire_time = trigger.get_next_fire_time(previous_fire_time, cursor)
        if fire_time is None or fire_time > now:
            break
        fire_times.append(fire_time)
        previous_fire_time = fire_time
        cursor = fire_time
    return fire_times


def first_start_occurrences(task: TaskConfig, now: datetime) -> list[datetime]:
    """On first Scheduler start, consider only today's already-due occurrence."""

    # 首次启动前没有责任边界，不反推并制造历史 missed 记录。
    planned_at = planned_at_for_task(task, now.date())
    return [planned_at] if planned_at <= now else []


def group_enabled_tasks(config: AppConfig) -> dict[str, list[TaskConfig]]:
    """Group enabled tasks by identical cron so one Chrome batch runs serially."""

    # 相同 cron 的任务共享一次浏览器启动和鉴权预检。
    grouped_tasks: dict[str, list[TaskConfig]] = defaultdict(list)
    for task in config.tasks:
        if task.enabled:
            grouped_tasks[task.schedule].append(task)
    return dict(grouped_tasks)


def mark_missed_tasks(
    database: Database,
    runtime_logger: RuntimeLogger,
    tasks: list[TaskConfig],
    planned_at: datetime,
    recorded_at: datetime,
    error_category: str,
) -> None:
    """Persist and log idempotent missed states for one planned task group."""

    # 同一 missed 处理使用一个通知批次 ID 汇总多任务结果。
    notification_batch_id = uuid4().hex
    # 只汇总本次真正新建 missed 终态的任务。
    missed_results: list[TaskNotificationResult] = []
    for task in tasks:
        # 数据库存在任意终态时不会创建重复 missed。
        # 持久化返回的是顶层 CollectionBatch.id，不是分类运行 ID。
        persisted_batch_id = database.record_missed_run(
            task_id=task.id,
            business_date=planned_at.date(),
            planned_at=planned_at,
            error_category=error_category,
            recorded_at=recorded_at,
        )
        if persisted_batch_id is None:
            continue
        missed_results.append(
            TaskNotificationResult(
                task_id=task.id,
                display_name=task.display_name,
                status=TaskNotificationStatus.MISSED,
                error_category=error_category,
            )
        )
        runtime_logger.emit(
            level="WARNING",
            event="scheduled_task_missed",
            message=(
                f"[{task.id}] 计划时间 {planned_at:%Y-%m-%d %H:%M} "
                f"已标记 missed，category={error_category}"
            ),
            stage="scheduling",
            context=LogContext(
                batch_id=persisted_batch_id,
                execution_batch_id=notification_batch_id,
                task_id=task.id,
            ),
            details={
                "planned_at": planned_at.isoformat(),
                "error_category": error_category,
            },
        )
    if missed_results:
        # 一个计划时刻的 missed 任务只发送一条 Scheduler 汇总。
        deliver_batch_notification(
            BatchNotificationSummary(
                batch_id=notification_batch_id,
                source=BatchSource.SCHEDULER,
                mode=BatchMode.OFFICIAL,
                started_at=recorded_at,
                finished_at=recorded_at,
                tasks=tuple(missed_results),
            ),
            runtime_logger,
        )


def handle_occurrence(
    config: AppConfig,
    database: Database,
    runtime_logger: RuntimeLogger,
    tasks: list[TaskConfig],
    planned_at: datetime,
    now: datetime,
    run_callback: ScheduledRunCallback,
) -> None:
    """Apply terminal idempotence, grace, and cross-day rules to one occurrence."""

    # 已有任意终态的任务不会被 Scheduler 自动重跑。
    pending_tasks = [
        task
        for task in tasks
        if not database.has_terminal_run(task.id, planned_at)
    ]
    if not pending_tasks:
        return
    if planned_at.date() != now.date() and not config.scheduler.cross_day_backfill:
        mark_missed_tasks(
            database,
            runtime_logger,
            pending_tasks,
            planned_at,
            now,
            "cross_day_missed",
        )
        return
    # 同日延迟超过宽限后只记 missed，不打开 Chrome。
    grace_window = timedelta(minutes=config.scheduler.misfire_grace_minutes)
    if now - planned_at > grace_window:
        mark_missed_tasks(
            database,
            runtime_logger,
            pending_tasks,
            planned_at,
            now,
            "misfire_grace_expired",
        )
        return
    # 正常或同日宽限补采由共享 runner 串行执行。
    run_callback(config, pending_tasks, planned_at)


def reconcile_scheduler_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
    run_callback: ScheduledRunCallback = run_scheduled_collection,
    runtime_logger: RuntimeLogger | None = None,
) -> None:
    """Reconcile downtime occurrences and durably advance every task checkpoint."""

    # 测试可注入时间，真实运行固定使用北京时间。
    current_time = now or datetime.now(SHANGHAI_TIMEZONE)
    upgrade_database(config.database.path)
    # 一次 reconcile 使用短生命周期数据库连接。
    database = Database(config.database.path)
    # missed 事件写入与采集一致的安全 JSONL。
    # GUI 子进程可复用同一个日志器，默认路径保持既有终端行为。
    active_logger = runtime_logger or RuntimeLogger(RUNTIME_ROOT / "logs")
    try:
        # 计划时刻映射让相同时间的任务共享一个浏览器批次。
        occurrence_tasks: dict[datetime, list[TaskConfig]] = defaultdict(list)
        # 每个任务只在其所有到期时刻处理完成后推进检查点。
        enabled_tasks = [task for task in config.tasks if task.enabled]
        for task in enabled_tasks:
            # 首次运行不追溯 Scheduler 启用之前的历史日期。
            checkpoint = database.scheduler_checkpoint(task.id)
            fire_times = (
                first_start_occurrences(task, current_time)
                if checkpoint is None
                else enumerate_fire_times(
                    task,
                    aware_checkpoint(checkpoint),
                    current_time,
                )
            )
            for fire_time in fire_times:
                occurrence_tasks[fire_time].append(task)
        for planned_at in sorted(occurrence_tasks):
            # 不同计划时刻严格按时间顺序串行处理。
            handle_occurrence(
                config,
                database,
                active_logger,
                occurrence_tasks[planned_at],
                planned_at,
                current_time,
                run_callback,
            )
        for task in enabled_tasks:
            database.set_scheduler_checkpoint(task.id, current_time)
    finally:
        database.close()


def _run_scheduler_unlocked(config: AppConfig) -> int:
    """Run a foreground APScheduler with serial cron jobs and startup reconciliation."""

    # Scheduler 生命周期事件写入现有按日 JSONL，而不是另建固定守护日志。
    runtime_logger = RuntimeLogger(RUNTIME_ROOT / "logs")
    # 终止信号只停止未来调度，正在运行的批次允许安全完成。
    shutdown_requested = Event()
    # 当前批次控制器用于 GUI 显式“中止当前采集”。
    active_control: list[CollectionControl | None] = [None]

    def run_group_with_control(
        callback_config: AppConfig,
        callback_tasks: list[TaskConfig],
        callback_planned_at: datetime,
    ) -> int:
        """Execute one scheduled group with a signal-addressable control object."""

        # 每个计划批次使用独立停止信号，不能污染下一次执行。
        group_control = CollectionControl(keep_browser_open=False)
        active_control[0] = group_control
        runtime_logger.emit(
            level="INFO",
            event="scheduled_group_started",
            message="定时采集批次开始",
            stage="scheduling",
        )
        try:
            return run_scheduled_collection(
                callback_config,
                callback_tasks,
                callback_planned_at,
                control=group_control,
            )
        finally:
            active_control[0] = None
            runtime_logger.emit(
                level="INFO",
                event="scheduled_group_finished",
                message="定时采集批次结束",
                stage="scheduling",
            )

    def request_graceful_shutdown(signum, frame) -> None:
        """Record SIGTERM so reconciliation or APScheduler can stop gracefully."""

        shutdown_requested.set()

    def request_active_interruption(signum, frame) -> None:
        """Forward SIGUSR1 only to the currently running scheduled collection."""

        # Scheduler 空闲时不保留中止信号，避免误伤下一次计划。
        current_control = active_control[0]
        if current_control is not None:
            current_control.request_stop()

    # GUI QProcess terminate 使用 SIGTERM，立即中止使用显式 SIGUSR1。
    previous_term_handler = signal.signal(signal.SIGTERM, request_graceful_shutdown)
    previous_interrupt_handler = signal.signal(signal.SIGUSR1, request_active_interruption)
    # 启动时先补齐进程停止期间的到期或 missed 状态。
    reconcile_scheduler_once(
        config,
        run_callback=run_group_with_control,
        runtime_logger=runtime_logger,
    )
    if shutdown_requested.is_set():
        runtime_logger.emit(
            level="INFO",
            event="scheduler_stopped",
            message="Scheduler 已停止",
            stage="scheduling",
        )
        signal.signal(signal.SIGTERM, previous_term_handler)
        signal.signal(signal.SIGUSR1, previous_interrupt_handler)
        return 0
    # 单工作线程保证不同 cron 组也不会并发操作同一 Chrome Profile。
    executors = {"default": ThreadPoolExecutor(max_workers=1)}
    # 调度器本身使用配置中已限制的北京时间。
    scheduler = BlockingScheduler(
        timezone=ZoneInfo(config.scheduler.timezone),
        executors=executors,
    )
    # job_id 到任务组的映射用于 missed 事件落库。
    job_tasks: dict[str, list[TaskConfig]] = {}
    for index, (schedule, tasks) in enumerate(group_enabled_tasks(config).items(), start=1):
        # 稳定短 job_id 不暴露筛选参数或认证信息。
        job_id = f"task_group_{index}"
        job_tasks[job_id] = tasks

        def execute_group(
            grouped_tasks: list[TaskConfig] = tasks,
        ) -> None:
            """Execute one cron group using its exact same-day planned time."""

            # APScheduler 正常或宽限触发时以当前北京日期构造计划时间。
            actual_at = datetime.now(SHANGHAI_TIMEZONE)
            planned_at = planned_at_for_task(grouped_tasks[0], actual_at.date())
            # 极端跨零点误点时回退到上一业务日，由规则标记 missed。
            if planned_at > actual_at:
                planned_at = planned_at_for_task(
                    grouped_tasks[0], actual_at.date() - timedelta(days=1)
                )
            upgrade_database(config.database.path)
            # cron 回调使用独立数据库处理终态和宽限规则。
            callback_database = Database(config.database.path)
            try:
                handle_occurrence(
                    config,
                    callback_database,
                    RuntimeLogger(RUNTIME_ROOT / "logs"),
                    grouped_tasks,
                    planned_at,
                    actual_at,
                    run_group_with_control,
                )
                for grouped_task in grouped_tasks:
                    callback_database.set_scheduler_checkpoint(
                        grouped_task.id, actual_at
                    )
            finally:
                callback_database.close()

        # CronTrigger 直接使用任务的五段 cron 和北京时区。
        trigger = CronTrigger.from_crontab(schedule, timezone=SHANGHAI_TIMEZONE)
        scheduler.add_job(
            execute_group,
            trigger=trigger,
            id=job_id,
            coalesce=False,
            max_instances=1,
            misfire_grace_time=config.scheduler.misfire_grace_minutes * 60,
        )

    def record_missed_event(event: JobExecutionEvent) -> None:
        """Persist APScheduler occurrences dropped after their grace window."""

        # 非 missed 事件和未知 job_id 不参与处理。
        tasks = job_tasks.get(event.job_id)
        if tasks is None:
            return
        # APScheduler 提供的 scheduled_run_time 是准确计划时刻。
        recorded_at = datetime.now(SHANGHAI_TIMEZONE)
        upgrade_database(config.database.path)
        # missed listener 使用独立短事务数据库。
        missed_database = Database(config.database.path)
        try:
            mark_missed_tasks(
                missed_database,
                RuntimeLogger(RUNTIME_ROOT / "logs"),
                tasks,
                event.scheduled_run_time.astimezone(SHANGHAI_TIMEZONE),
                recorded_at,
                "misfire_grace_expired",
            )
            for task in tasks:
                missed_database.set_scheduler_checkpoint(task.id, recorded_at)
        finally:
            missed_database.close()

    scheduler.add_listener(record_missed_event, EVENT_JOB_MISSED)
    runtime_logger.emit(
        level="INFO",
        event="scheduler_started",
        message="Scheduler 已启动，按 Ctrl-C 停止",
        stage="scheduling",
    )
    try:
        # 轻量线程监听终止标记并调用 APScheduler 的等待式 shutdown。
        def stop_scheduler_when_requested() -> None:
            """Wait for SIGTERM and stop future jobs after the active job completes."""

            shutdown_requested.wait()
            # 信号可能恰好早于 scheduler.start，等待 running 避免丢失停止请求。
            while not scheduler.running:
                time.sleep(0.05)
            scheduler.shutdown(wait=True)

        # APScheduler 启动前创建守护监听线程，不阻止进程自然退出。
        shutdown_thread = Thread(
            target=stop_scheduler_when_requested,
            name="scheduler-graceful-stop",
            daemon=True,
        )
        shutdown_thread.start()
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        # 前台开发运行通过 Ctrl-C 正常退出，不等待 Enter。
        if scheduler.running:
            scheduler.shutdown(wait=True)
    finally:
        # 测试或嵌入调用结束后恢复进程原有信号处理器。
        signal.signal(signal.SIGTERM, previous_term_handler)
        signal.signal(signal.SIGUSR1, previous_interrupt_handler)
    runtime_logger.emit(
        level="INFO",
        event="scheduler_stopped",
        message="Scheduler 已停止",
        stage="scheduling",
    )
    return 0


def run_scheduler(config: AppConfig) -> int:
    """Run exactly one Scheduler process for this runtime directory."""

    # Scheduler 锁在空闲期持续持有，用于阻止终端、launchd 和 GUI 重复启动。
    scheduler_lock = ProcessLock(
        RUNTIME_ROOT / "locks" / SCHEDULER_LOCK_NAME,
        "scheduler",
    )
    with scheduler_lock:
        return _run_scheduler_unlocked(config)
