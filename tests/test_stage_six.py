"""Stage-six GUI routing, safe events, cancellation, and lock tests."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from compass_collector.cli import build_parser
from compass_collector.config import load_config
from compass_collector.errors import TaskCollectionError
from compass_collector.persistence import Database, upgrade_database
from compass_collector.run_control import CollectionControl
from compass_collector.runner import TaskExecutionPlan, collect_task
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy, lock_is_held
from compass_collector.runtime_logging import (
    EVENT_STREAM_ENV,
    EVENT_STREAM_PREFIX,
    LogContext,
    RuntimeLogger,
    read_latest_batch_events,
)


# 阶段六运行状态和调度仍统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_cli_run_defaults_to_gui_and_no_gui_is_explicit() -> None:
    """Keep run GUI-first while preserving one explicit terminal fallback."""

    # 默认 run 参数用于证明命令会由 cli.main 路由到 GUI。
    default_arguments = build_parser().parse_args(
        ["run", "--task", "product_hot_sale_drinks"]
    )
    # --no-gui 是终端与自动化的显式兼容入口。
    terminal_arguments = build_parser().parse_args(
        ["run", "--task", "product_hot_sale_drinks", "--no-gui"]
    )
    # app 只打开空闲控制台，不复用 run 的自动执行语义。
    app_arguments = build_parser().parse_args(
        ["app", "--task", "product_hot_sale_drinks"]
    )

    assert default_arguments.no_gui is False
    assert terminal_arguments.no_gui is True
    assert app_arguments.command == "app"


def test_process_locks_detect_live_owner_and_release_automatically(tmp_path: Path) -> None:
    """Distinguish a live advisory owner from a stale on-disk lock file."""

    # 同一路径模拟 GUI 或 Scheduler 的进程级单实例锁。
    lock_path = tmp_path / "runtime" / "locks" / "gui.lock"
    # first_lock 在上下文中持续持有文件描述符。
    first_lock = ProcessLock(lock_path, "gui")
    with first_lock:
        assert lock_is_held(lock_path, "gui") is True
        # 第二个对象不能获得同一个实时锁。
        with pytest.raises(RuntimeLockBusy):
            ProcessLock(lock_path, "gui").acquire()
    # 文件仍存在不代表锁仍被占用，判断只依赖操作系统锁。
    assert lock_path.exists()
    assert lock_is_held(lock_path, "gui") is False


def test_collection_control_separates_stop_and_browser_close() -> None:
    """Keep cooperative collection cancellation independent from Chrome inspection."""

    # 新控制器默认既未中止，也未允许关闭保留的 Chrome。
    control = CollectionControl(keep_browser_open=True)
    assert control.stop_requested() is False
    assert control.wait_for_delay(0) is False

    control.request_stop()
    assert control.stop_requested() is True
    assert control.wait_for_delay(10) is True
    # 浏览器关闭是单独动作，不会清除已经发生的中止请求。
    control.request_browser_close()
    control.wait_for_browser_close()
    assert control.stop_requested() is True


def test_logger_sink_and_history_use_the_same_safe_events(tmp_path: Path) -> None:
    """Send live GUI events while keeping JSONL as the only persistent record."""

    # sink_events 模拟 Qt Signal 接收到的结构化 payload。
    sink_events: list[dict] = []
    logger = RuntimeLogger(tmp_path / "logs", event_sink=sink_events.append)
    # 两个批次证明恢复函数只选择最新真实批次。
    first_context = LogContext(batch_id="batch-one", run_id="run-one", task_id="task")
    second_context = LogContext(batch_id="batch-two", run_id="run-two", task_id="task")
    logger.emit(
        level="INFO",
        event="task_started",
        message="第一批次",
        stage="collection",
        context=first_context,
    )
    logger.emit(
        level="INFO",
        event="page_collected",
        message="第二批次第 1 页",
        stage="collection",
        context=second_context,
        details={"page_no": 1, "target_pages": 2},
    )
    logger.emit(
        level="INFO",
        event="page_collected",
        message="第二批次第 2 页",
        stage="collection",
        context=second_context,
        details={"page_no": 2, "target_pages": 2},
    )

    # 实时 sink 与落盘恢复共享同一事件字段，不形成第二份日志文件。
    restored_events = read_latest_batch_events(tmp_path / "logs", limit=500)
    assert len(sink_events) == 3
    assert [event["batch_id"] for event in restored_events] == [
        "batch-two",
        "batch-two",
    ]
    assert len(list((tmp_path / "logs").glob("*.jsonl"))) == 1


def test_event_stream_outputs_only_prefixed_safe_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Provide QProcess a structured stream without duplicating readable messages."""

    # GUI Scheduler 子进程通过环境变量选择结构化 stdout。
    monkeypatch.setenv(EVENT_STREAM_ENV, "1")
    logger = RuntimeLogger(tmp_path / "logs")
    logger.emit(
        level="INFO",
        event="scheduler_started",
        message="Scheduler 已启动",
        stage="scheduling",
    )

    # stdout 必须只有一个带固定前缀的安全 JSON 事件。
    output_line = capsys.readouterr().out.strip()
    assert output_line.startswith(EVENT_STREAM_PREFIX)
    assert output_line.count("Scheduler 已启动") == 1


def test_pre_requested_stop_creates_interrupted_manifest_without_http(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Stop before page one and never publish or call the verified endpoint."""

    # runner runtime 指向临时目录，避免创建真实原始响应。
    monkeypatch.setattr("compass_collector.runner.RUNTIME_ROOT", tmp_path / "runtime")
    config = load_config(Path("config/tasks.yaml"))
    # 固定执行计划只用于进入 collect_task 的协作式停止边界。
    task = config.tasks[0]
    planned_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    plan = TaskExecutionPlan(
        task=task,
        business_date=date(2026, 7, 16),
        planned_at=planned_at,
        version=1,
    )
    # stop 在工作开始前设置，HTTP 客户端不应被调用。
    control = CollectionControl()
    control.request_stop()

    class FailingClient:
        """Fail the test if cancellation reaches the HTTP boundary."""

        def get_page(self, selected_task, params):
            """Prove no request occurs after a pre-requested stop."""

            raise AssertionError("HTTP should not be called")

    with pytest.raises(TaskCollectionError) as captured_error:
        collect_task(
            plan,
            config,
            FailingClient(),
            RuntimeLogger(tmp_path / "runtime" / "logs"),
            "batch-interrupted",
            control,
        )

    # 中止是独立终态，不伪装为成功或普通失败。
    assert captured_error.value.storage.manifest["status"] == "interrupted"
    assert captured_error.value.storage.manifest["saved_pages"] == 0


def test_skipped_busy_is_a_terminal_status_without_csv(tmp_path: Path) -> None:
    """Persist a busy Scheduler occurrence without publishing or retrying it."""

    # 临时数据库隔离正式 runtime 数据。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    # 固定计划时间便于核对终态幂等。
    planned_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    try:
        first_run_id = database.record_skipped_busy_run(
            task_id="product_hot_sale_drinks",
            business_date=date(2026, 7, 16),
            planned_at=planned_at,
            recorded_at=planned_at,
        )
        duplicate_run_id = database.record_skipped_busy_run(
            task_id="product_hot_sale_drinks",
            business_date=date(2026, 7, 16),
            planned_at=planned_at,
            recorded_at=planned_at,
        )
        status_rows = database.recent_status(limit=5)
    finally:
        database.close()

    assert first_run_id is not None
    assert duplicate_run_id is None
    assert len(status_rows) == 1
    assert status_rows[0].status == "skipped_busy"
    assert status_rows[0].error_category == "skipped_busy"
    assert status_rows[0].csv_path is None


def test_makefile_exposes_gui_and_terminal_fallback_commands() -> None:
    """Keep the phase-six entrypoints visible as npm-style scripts."""

    # Makefile 是日常执行入口的公开契约。
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "app:" in makefile
    assert "run-cli:" in makefile
    assert "dry-run-cli:" in makefile
    assert "force-cli:" in makefile
    assert "notify-test:" in makefile
    assert "--no-gui" in makefile
