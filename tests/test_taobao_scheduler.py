"""Real shared scheduling/database/lock checks with both platform contracts."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from compass_collector import runner, scheduler
from compass_collector.persistence import CollectionBatch, Database
from compass_collector.runtime_locks import ProcessLock
from compass_collector.runtime_logging import RuntimeLogger
from test_dynamic_scheduler import build_test_config, create_test_database

# 测试时钟采用同一个业务时区，不依赖宿主机当前时间。
ZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Enable both defaults only in isolated tests and prohibit real notifications."""
    # 默认配置的淘宝保持禁用；测试副本用于模拟正式验收后启用的任务组。
    configured = build_test_config(tmp_path)
    configured = configured.model_copy(update={
        "tasks": [task.model_copy(update={"enabled": True}) for task in configured.tasks],
    })
    monkeypatch.setattr(runner, "RUNTIME_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(scheduler, "deliver_batch_notification", lambda *args: None)
    monkeypatch.setattr(runner, "deliver_batch_notification", lambda *args: None)
    return configured


def test_same_day_grace_dispatches_both_platforms_in_one_ordered_group(config, tmp_path):
    """Reconciliation groups equal schedules and advances durable checkpoints once."""
    # 真实调度入口只替换最终浏览器执行回调，数据库和时间判断保持真实。
    dispatches = []
    now = datetime(2026, 10, 1, 14, 5, tzinfo=ZONE)
    planned = now.replace(minute=0)

    def dispatch(actual_config, tasks, planned_at):
        """Record exact task order without opening Chrome."""
        dispatches.append(([task.platform for task in tasks], planned_at))
        return 0

    for instant in (now, now + timedelta(minutes=1)):
        scheduler.reconcile_scheduler_once(config, now=instant, run_callback=dispatch,
                                            runtime_logger=RuntimeLogger(tmp_path / "logs"))
    assert dispatches == [(["compass", "taobao"], planned)]


def test_cross_day_missed_preserves_each_platform_and_snapshot(config, tmp_path, monkeypatch):
    """A missed Taobao occurrence must not become a Compass audit record."""
    # 真实数据库和漏跑处理验证平台归属、无发布以及幂等收口。
    database = create_test_database(config)
    planned = datetime(2026, 9, 30, 14, 0, tzinfo=ZONE)
    logger = RuntimeLogger(tmp_path / "logs")
    # 漏跑通知必须保留两平台身份且只发送一次，不触发真实Webhook。
    summaries = []
    monkeypatch.setattr(scheduler, "deliver_batch_notification", lambda summary, *args: summaries.append(summary))
    try:
        for _ in range(2):
            scheduler.handle_occurrence(config, database, logger, config.tasks, planned,
                                         planned + timedelta(days=1),
                                         lambda *args: pytest.fail("cross-day dispatch"))
        with database.session_factory() as session:
            # 直接检查 ORM 平台及配置快照，不仅检查日志展示名称。
            rows = session.scalars(select(CollectionBatch)).all()
            assert len(rows) == 2
            assert {row.platform for row in rows} == {"compass", "taobao"}
            for row in rows:
                assert row.config_snapshot["platform"] == row.platform
                assert row.config_snapshot["id"] == row.task_id
                assert row.status == "missed"
                assert row.error_category == "cross_day_missed"
                assert row.published_at is None and row.csv_path is None
        assert len(summaries) == 1
        assert [task.platform for task in summaries[0].tasks] == ["compass", "taobao"]
    finally:
        database.close()


def test_collection_lock_blocks_both_platforms_without_losing_identity(config, tmp_path):
    """The actual shared process lock produces correctly attributed busy terminals."""
    # 锁由真实实现持有，runner必须在打开任何平台浏览器前安全跳过。
    planned = datetime(2026, 10, 1, 14, 0, tzinfo=ZONE)
    with ProcessLock(tmp_path / "runtime" / "locks" / runner.COLLECTION_LOCK_NAME, "collection"):
        assert runner.run_scheduled_collection(config, config.tasks, planned) == 0
        assert runner.run_scheduled_collection(config, config.tasks, planned) == 0
    database = Database(config.database.path)
    try:
        with database.session_factory() as session:
            # 相同计划和锁冲突不能重复插入终态；两平台各一行。
            rows = session.scalars(select(CollectionBatch)).all()
            assert len(rows) == 2
            for row in rows:
                assert row.platform == row.config_snapshot["platform"]
                assert row.status == "skipped_busy"
                assert row.published_at is None
    finally:
        database.close()
