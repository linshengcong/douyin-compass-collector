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


def test_same_day_grace_dispatches_platforms_in_independent_groups(config, tmp_path):
    """Reconciliation groups equal schedules and advances durable checkpoints once."""
    # 真实调度入口只替换最终浏览器执行回调，数据库和时间判断保持真实。
    dispatches = []
    now = datetime(2026, 10, 1, 14, 5, tzinfo=ZONE)
    planned = now.replace(minute=0)

    def dispatch(actual_config, tasks, planned_at):
        """Record exact task order without opening Chrome."""
        dispatches.append(([task.platform for task in tasks], planned_at))
        return 0

    for platform in ("compass", "taobao"):
        selected = config.for_platform(platform)
        for instant in (now, now + timedelta(minutes=1)):
            scheduler.reconcile_scheduler_once(selected, now=instant, run_callback=dispatch,
                                                runtime_logger=RuntimeLogger(tmp_path / "logs" / platform, platform=platform))
    assert dispatches == [(["compass"], planned), (["taobao"], planned)]


def test_cross_day_missed_preserves_each_platform_and_snapshot(config, tmp_path, monkeypatch):
    """A missed Taobao occurrence must not become a Compass audit record."""
    # 真实数据库和漏跑处理验证平台归属、无发布以及幂等收口。
    planned = datetime(2026, 9, 30, 14, 0, tzinfo=ZONE)
    # 每个平台独立记录漏跑和通知，不共享批次或归属表。
    summaries = []
    monkeypatch.setattr(scheduler, "deliver_batch_notification", lambda summary, *args: summaries.append(summary))
    for platform in ("compass", "taobao"):
        selected = config.for_platform(platform)
        database = create_test_database(selected)
        logger = RuntimeLogger(tmp_path / "logs" / platform, platform=platform)
        try:
            for _ in range(2):
                scheduler.handle_occurrence(selected, database, logger, selected.tasks, planned,
                                             planned + timedelta(days=1),
                                             lambda *args: pytest.fail("cross-day dispatch"))
            with database.session_factory() as session:
                rows = session.scalars(select(CollectionBatch)).all()
                assert len(rows) == 1 and rows[0].platform == platform
                assert rows[0].config_snapshot["platform"] == platform
                assert rows[0].status == "missed" and rows[0].published_at is None
        finally:
            database.close()
    assert [[task.platform for task in summary.tasks] for summary in summaries] == [["compass"], ["taobao"]]


def test_collection_lock_blocks_both_platforms_without_losing_identity(config, tmp_path):
    """The actual shared process lock produces correctly attributed busy terminals."""
    # 锁由真实实现持有，runner必须在打开任何平台浏览器前安全跳过。
    planned = datetime(2026, 10, 1, 14, 0, tzinfo=ZONE)
    for platform in ("compass", "taobao"):
        selected = config.for_platform(platform)
        # 旧全局锁仍然禁止新进程启动，但各自终态只写入独立数据库。
        with ProcessLock(tmp_path / "runtime" / "locks" / runner.COLLECTION_LOCK_NAME, "collection"):
            assert runner.run_scheduled_collection(selected, selected.tasks, planned) == 0
            assert runner.run_scheduled_collection(selected, selected.tasks, planned) == 0
        database = Database(selected.database.url)
        try:
            with database.session_factory() as session:
                rows = session.scalars(select(CollectionBatch)).all()
                assert len(rows) == 1 and rows[0].platform == platform
                assert rows[0].status == "skipped_busy" and rows[0].published_at is None
        finally:
            database.close()
