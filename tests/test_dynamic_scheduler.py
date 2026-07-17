"""Dynamic-category Scheduler publication and log-identity tests."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from compass_collector.config import AppConfig, TaskConfig, load_config
from compass_collector.notifier import BatchNotificationSummary
from compass_collector.persistence import CollectionBatch, Database, upgrade_database
from compass_collector.runtime_logging import RuntimeLogger
from compass_collector.scheduler import handle_occurrence, mark_missed_tasks


# 真实任务配置用于保持 Scheduler 规则与生产入口一致。
CONFIG_PATH = Path("config/tasks.yaml")
# 测试计划时间统一使用工程确认的北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def build_test_config(tmp_path: Path) -> AppConfig:
    """Point the checked-in task configuration at one isolated SQLite database."""

    # 真实配置只替换数据库路径，cron 和动态分类范围保持不变。
    config = load_config(CONFIG_PATH)
    # 临时数据库配置避免专项测试读写仓库 runtime。
    database_config = config.database.model_copy(
        update={"path": tmp_path / "runtime" / "data" / "collector.db"}
    )
    return config.model_copy(update={"database": database_config})


def create_test_database(config: AppConfig) -> Database:
    """Create the clean current schema required by Scheduler status queries."""

    # 每个 pytest 临时配置都先升级到当前干净 v1 基线。
    upgrade_database(config.database.path)
    return Database(config.database.path)


def insert_partial_success_batch(
    database: Database,
    *,
    batch_id: str,
    task_id: str,
    planned_at: datetime,
    mode: Literal["normal", "dry_run"],
) -> None:
    """Insert one constraint-valid partial-success batch for scheduling decisions."""

    # SQLite 按北京墙上时间保存无时区 datetime。
    stored_planned_at = planned_at.replace(tzinfo=None)
    # 完成时间固定晚于开始时间，满足终态生命周期约束。
    finished_at = stored_planned_at + timedelta(minutes=10)
    # 只有 normal 模式代表已经正式发布的 partial_success。
    is_official = mode == "normal"
    # 批次直接写入当前 ORM，测试不需要构造任何浏览器或网络对象。
    batch = CollectionBatch(
        id=batch_id,
        task_id=task_id,
        business_date=planned_at.date(),
        planned_at=stored_planned_at,
        mode=mode,
        status="partial_success",
        version=1 if is_official else None,
        root_category_id="13",
        root_category_name="食品饮料",
        manifest_path=f"runtime/raw/{batch_id}/manifest.json",
        category_tree_raw_path=f"runtime/raw/{batch_id}/category-tree.json.gz",
        csv_path=f"runtime/exports/{batch_id}.csv" if is_official else None,
        discovered_category_count=2,
        successful_category_count=1,
        failed_category_count=1,
        not_started_category_count=0,
        saved_page_count=3,
        collected_item_count=20,
        error_category=None,
        started_at=stored_planned_at,
        finished_at=finished_at,
        published_at=finished_at if is_official else None,
    )
    with database.session_factory.begin() as session:
        session.add(batch)


def test_official_partial_success_is_terminal_and_skips_scheduler_dispatch(
    tmp_path: Path,
) -> None:
    """Do not retry a partial result that already crossed the publication boundary."""

    # 临时配置和数据库隔离正式 Scheduler 状态。
    config = build_test_config(tmp_path)
    database = create_test_database(config)
    # 当前唯一启用任务用于构造准确计划时刻。
    task = config.tasks[0]
    # 正式部分成功批次属于同一任务和同一计划时间。
    planned_at = datetime(2026, 7, 17, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    insert_partial_success_batch(
        database,
        batch_id="official-partial-batch",
        task_id=task.id,
        planned_at=planned_at,
        mode="normal",
    )

    def fail_if_dispatched(
        callback_config: AppConfig,
        callback_tasks: list[TaskConfig],
        callback_planned_at: datetime,
    ) -> int:
        """Fail immediately if an already-published occurrence reaches the runner."""

        raise AssertionError("official partial_success must not be dispatched")

    try:
        # 数据库终态判定必须直接识别正式 partial_success。
        assert database.has_terminal_run(task.id, planned_at) is True
        # 调度入口也必须在调用 runner 之前跳过该任务。
        handle_occurrence(
            config,
            database,
            RuntimeLogger(tmp_path / "runtime" / "logs"),
            [task],
            planned_at,
            planned_at + timedelta(minutes=5),
            fail_if_dispatched,
        )
    finally:
        database.close()


def test_dry_run_partial_success_does_not_block_official_scheduler_dispatch(
    tmp_path: Path,
) -> None:
    """Allow an official occurrence after an earlier dry-run partial success."""

    # 临时配置和数据库隔离正式 Scheduler 状态。
    config = build_test_config(tmp_path)
    database = create_test_database(config)
    # 当前唯一启用任务用于构造准确计划时刻。
    task = config.tasks[0]
    # dry-run 与正式计划可以共享同一个 planned_at。
    planned_at = datetime(2026, 7, 17, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    insert_partial_success_batch(
        database,
        batch_id="dry-run-partial-batch",
        task_id=task.id,
        planned_at=planned_at,
        mode="dry_run",
    )
    # 回调记录真实调度参数，但不会打开 Chrome 或请求网络。
    dispatches: list[tuple[list[str], datetime]] = []

    def record_dispatch(
        callback_config: AppConfig,
        callback_tasks: list[TaskConfig],
        callback_planned_at: datetime,
    ) -> int:
        """Record one allowed official dispatch for deterministic assertions."""

        dispatches.append(
            ([callback_task.id for callback_task in callback_tasks], callback_planned_at)
        )
        return 0

    try:
        # dry-run 终态不能占用 Scheduler 的正式执行责任。
        assert database.has_terminal_run(task.id, planned_at) is False
        handle_occurrence(
            config,
            database,
            RuntimeLogger(tmp_path / "runtime" / "logs"),
            [task],
            planned_at,
            planned_at + timedelta(minutes=5),
            record_dispatch,
        )
    finally:
        database.close()

    assert dispatches == [([task.id], planned_at)]


def test_missed_log_uses_persisted_batch_without_fake_category_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Scheduler-only batch identity out of the category-run log field."""

    # 临时配置和数据库隔离 missed 终态记录。
    config = build_test_config(tmp_path)
    database = create_test_database(config)
    # 当前唯一启用任务用于创建一个 Scheduler-only missed 批次。
    task = config.tasks[0]
    # 计划与记录时间固定，便于查询唯一状态行。
    planned_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    recorded_at = datetime(2026, 7, 17, 9, 0, tzinfo=SHANGHAI_TIMEZONE)
    # 事件 sink 直接接收已经通过安全字段审查的 payload。
    events: list[dict] = []
    # 通知汇总只记录调用，不执行真实 Webhook。
    notification_summaries: list[BatchNotificationSummary] = []

    def record_notification(summary, runtime_logger) -> None:
        """Capture one notification summary without network access."""

        notification_summaries.append(summary)

    monkeypatch.setattr(
        "compass_collector.scheduler.deliver_batch_notification",
        record_notification,
    )
    try:
        mark_missed_tasks(
            database,
            RuntimeLogger(tmp_path / "runtime" / "logs", event_sink=events.append),
            [task],
            planned_at,
            recorded_at,
            "cross_day_missed",
        )
        # 最近状态行提供本次持久化 CollectionBatch.id。
        status_rows = database.recent_status(limit=5)
    finally:
        database.close()

    # missed 日志事件应精确指向唯一持久化批次。
    missed_event = next(event for event in events if event["event"] == "scheduled_task_missed")
    assert len(status_rows) == 1
    assert missed_event["batch_id"] == status_rows[0].batch_id
    assert missed_event["category_run_id"] is None
    assert len(notification_summaries) == 1
    assert missed_event["execution_batch_id"] == notification_summaries[0].batch_id
