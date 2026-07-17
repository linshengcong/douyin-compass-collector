"""Stage-four durable scheduling, missed-state, and browser lifecycle tests."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from compass_collector.config import AppConfig, load_config
from compass_collector.persistence import Database, upgrade_database
from compass_collector.runner import run_collection
from compass_collector.scheduler import enumerate_fire_times, reconcile_scheduler_once


# 真实配置是阶段四测试的严格契约基线。
CONFIG_PATH = Path("config/tasks.yaml")
# 所有计划时间使用工程确认的北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def temporary_config(tmp_path: Path) -> AppConfig:
    """Point the real checked-in config at a test-only SQLite database."""

    # 真实配置模型只替换数据库路径，其余业务参数保持不变。
    config = load_config(CONFIG_PATH)
    # 嵌套 Pydantic 模型使用不可变式复制避免污染其他测试。
    database_config = config.database.model_copy(
        update={"path": tmp_path / "runtime" / "data" / "collector.db"}
    )
    return config.model_copy(update={"database": database_config})


def test_scheduler_config_and_daily_cron_are_strict() -> None:
    """Load the real scheduler boundary and reject unsupported cron semantics."""

    # 真实 YAML 用于核对北京时间和十小时宽限。
    config = load_config(CONFIG_PATH)
    assert config.scheduler.timezone == "Asia/Shanghai"
    assert config.scheduler.misfire_grace_minutes == 600
    assert config.scheduler.cross_day_backfill is False

    # 首版不接受需要额外业务日期定义的复杂 cron。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["tasks"][0]["schedule"] = "*/5 * * * *"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


def test_cron_enumerates_every_fire_after_checkpoint() -> None:
    """Recover each daily occurrence after a durable downtime checkpoint."""

    # 真实任务固定每天北京时间 14:00。
    task = load_config(CONFIG_PATH).tasks[0]
    # 两天前 15:00 到当前 15:00 应包含两个后续计划时刻。
    checkpoint = datetime(2026, 7, 14, 15, 0, tzinfo=SHANGHAI_TIMEZONE)
    now = datetime(2026, 7, 16, 15, 0, tzinfo=SHANGHAI_TIMEZONE)

    assert enumerate_fire_times(task, checkpoint, now) == [
        datetime(2026, 7, 15, 14, 0, tzinfo=SHANGHAI_TIMEZONE),
        datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE),
    ]


def test_first_reconcile_runs_today_once_and_advances_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Backfill a same-day first start once without inventing earlier history."""

    # 临时配置和日志目录隔离真实数据库与 runtime。
    config = temporary_config(tmp_path)
    monkeypatch.setattr("compass_collector.scheduler.RUNTIME_ROOT", tmp_path / "runtime")
    # 回调捕获到期任务但不启动真实 Chrome。
    calls: list[tuple[list[str], datetime]] = []

    def fake_run(
        callback_config: AppConfig,
        tasks,
        planned_at: datetime,
    ) -> int:
        """Record one scheduler dispatch for deterministic assertions."""

        calls.append(([task.id for task in tasks], planned_at))
        return 0

    # 当天 15:00 仍位于默认十小时宽限内。
    now = datetime(2026, 7, 16, 15, 0, tzinfo=SHANGHAI_TIMEZONE)
    reconcile_scheduler_once(config, now=now, run_callback=fake_run)
    # 同一时刻再次 reconcile 不得重复调用。
    reconcile_scheduler_once(config, now=now, run_callback=fake_run)

    assert calls == [
        (
            ["product_hot_sale_all_level3"],
            datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE),
        )
    ]
    # 持久化检查点证明第二次调用没有依赖进程内状态。
    database = Database(config.database.path)
    try:
        checkpoint = database.scheduler_checkpoint("product_hot_sale_all_level3")
    finally:
        database.close()
    assert checkpoint == datetime(2026, 7, 16, 15, 0)


def test_cross_day_occurrence_is_missed_and_never_dispatched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Record yesterday as missed without using today's real-time request date."""

    # 临时数据库先写入昨天计划时间之前的检查点。
    config = temporary_config(tmp_path)
    monkeypatch.setattr("compass_collector.scheduler.RUNTIME_ROOT", tmp_path / "runtime")
    upgrade_database(config.database.path)
    database = Database(config.database.path)
    try:
        database.set_scheduler_checkpoint(
            "product_hot_sale_all_level3",
            datetime(2026, 7, 15, 13, 0, tzinfo=SHANGHAI_TIMEZONE),
        )
    finally:
        database.close()
    # 当前仍早于今天 14:00，因此只枚举昨天一次。
    now = datetime(2026, 7, 16, 13, 0, tzinfo=SHANGHAI_TIMEZONE)
    calls: list[datetime] = []

    def fake_run(
        callback_config: AppConfig,
        tasks,
        planned_at: datetime,
    ) -> int:
        """Fail the test if a cross-day real-time collection is dispatched."""

        calls.append(planned_at)
        return 0

    reconcile_scheduler_once(config, now=now, run_callback=fake_run)

    database = Database(config.database.path)
    try:
        rows = database.recent_status(limit=5)
    finally:
        database.close()
    assert calls == []
    assert len(rows) == 1
    assert rows[0].status == "missed"
    assert rows[0].planned_at == datetime(2026, 7, 15, 14, 0)
    assert rows[0].error_category == "cross_day_missed"


def test_same_cron_tasks_dispatch_as_one_serial_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Group tasks sharing one planned time into a single runner invocation."""

    # 第二个任务复用已验证业务契约，只改变唯一任务 ID。
    config = temporary_config(tmp_path)
    first_task = config.tasks[0]
    second_task = first_task.model_copy(update={"id": "product_hot_sale_second"})
    config = config.model_copy(update={"tasks": [first_task, second_task]})
    monkeypatch.setattr("compass_collector.scheduler.RUNTIME_ROOT", tmp_path / "runtime")
    # 单次调用中的任务顺序用于验证严格串行批次边界。
    calls: list[list[str]] = []

    def fake_run(
        callback_config: AppConfig,
        tasks,
        planned_at: datetime,
    ) -> int:
        """Capture one grouped runner invocation without opening Chrome."""

        calls.append([task.id for task in tasks])
        return 0

    # 两个任务在同一天同一 14:00 计划时刻到期。
    now = datetime(2026, 7, 16, 15, 0, tzinfo=SHANGHAI_TIMEZONE)
    reconcile_scheduler_once(config, now=now, run_callback=fake_run)

    assert calls == [["product_hot_sale_all_level3", "product_hot_sale_second"]]


def test_scheduler_auth_failure_closes_browser_without_waiting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Persist auth_required, block the batch, and auto-close scheduler Chrome."""

    # 临时配置和 runner runtime 隔离真实 Profile 与数据库。
    config = temporary_config(tmp_path)
    monkeypatch.setattr("compass_collector.runner.RUNTIME_ROOT", tmp_path / "runtime")
    # 生命周期计数证明没有调用人工等待入口。
    lifecycle = {"closed": 0, "waited": 0}

    class FakeBrowserSession:
        """Expose only the browser methods reached before missing-auth handling."""

        def whitelisted_cookies(self, cookie_names):
            """Return no authentication state to trigger batch blocking."""

            return []

        def wait_for_manual_exit(self, message: str) -> None:
            """Record an invalid Scheduler wait instead of blocking the test."""

            lifecycle["waited"] += 1

        def close(self) -> None:
            """Record automatic browser cleanup."""

            lifecycle["closed"] += 1

    # 浏览器启动替换为无 Cookie 的轻量会话。
    monkeypatch.setattr(
        "compass_collector.runner.open_browser",
        lambda browser_config: FakeBrowserSession(),
    )
    # Scheduler 使用准确计划时间而不是当前测试日期重新计算。
    task = config.tasks[0]
    # 第二个任务用于验证批次级鉴权阻断会写入全部终态。
    second_task = task.model_copy(update={"id": "product_hot_sale_second"})
    planned_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    exit_code = run_collection(
        config,
        selected_task_id=None,
        force=False,
        dry_run=False,
        manual=False,
        scheduled_tasks=[task, second_task],
        planned_at_overrides={
            task.id: planned_at,
            second_task.id: planned_at,
        },
    )

    database = Database(config.database.path)
    try:
        rows = database.recent_status(limit=5)
    finally:
        database.close()
    assert exit_code == 1
    assert lifecycle == {"closed": 1, "waited": 0}
    assert len(rows) == 2
    assert {row.task_id for row in rows} == {
        "product_hot_sale_all_level3",
        "product_hot_sale_second",
    }
    assert {row.status for row in rows} == {"auth_required"}
    assert {row.error_category for row in rows} == {"auth_required"}
    # 每个 TaskExecutionPlan 必须拥有独立 collection batch 主键。
    assert len({row.batch_id for row in rows}) == 2
