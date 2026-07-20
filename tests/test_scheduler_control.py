"""Cross-platform Scheduler control-channel regression tests."""

from pathlib import Path
import signal
import time
from threading import Event
from types import SimpleNamespace

import pytest

from compass_collector import gui as gui_module
from compass_collector import scheduler as scheduler_module
from compass_collector.gui import CollectorWindow
from compass_collector.run_control import CollectionControl
from compass_collector.scheduler import apply_scheduler_control_requests
from compass_collector.scheduler_control import (
    SCHEDULER_CONTROL_ID_ENV,
    SchedulerControlFiles,
)


def test_control_requests_are_one_shot_and_instance_scoped(tmp_path: Path) -> None:
    """Do not let an old Scheduler request affect a later owned process."""

    # 两个实例共用 controls 目录，模拟 GUI 连续启动 Scheduler。
    first_control = SchedulerControlFiles(tmp_path, "a" * 32)
    second_control = SchedulerControlFiles(tmp_path, "b" * 32)

    # 中止请求只应由目标实例消费一次。
    first_control.request_interruption()
    assert first_control.consume_interruption() is True
    assert first_control.consume_interruption() is False
    assert second_control.consume_interruption() is False

    # 优雅停止与中止使用独立请求，不能互相吞掉。
    second_control.request_shutdown()
    assert second_control.consume_interruption() is False
    assert second_control.consume_shutdown() is True


def test_scheduler_forwards_interrupt_without_aborting_graceful_shutdown(
    tmp_path: Path,
) -> None:
    """Keep active interruption and graceful Scheduler shutdown independent."""

    # 真实采集控制器验证中止请求到达 runner 使用的协作边界。
    collection_control = CollectionControl(keep_browser_open=False)
    # Scheduler 停止事件单独记录未来调度关闭请求。
    shutdown_requested = Event()
    # 同一实例同时收到两种请求，模拟用户先停止再中止慢批次。
    control_files = SchedulerControlFiles(tmp_path, "e" * 32)
    control_files.request_interruption()
    control_files.request_shutdown()

    apply_scheduler_control_requests(
        control_files,
        shutdown_requested,
        collection_control,
    )

    assert collection_control.stop_requested() is True
    assert shutdown_requested.is_set() is True


def test_scheduler_start_and_stop_does_not_require_sigusr1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start the Scheduler control path when Windows exposes no SIGUSR1."""

    # 固定实例 ID 让测试能在调用前写入停止请求。
    control_id = "c" * 32
    # 运行目录隔离请求文件和日志文件。
    monkeypatch.setattr(scheduler_module, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setenv(SCHEDULER_CONTROL_ID_ENV, control_id)
    # 预先请求停止，Scheduler 完成启动协调后应直接安全退出。
    control_files = SchedulerControlFiles(tmp_path / "controls", control_id)
    control_files.request_shutdown()
    # Windows 症状由移除当前平台的 SIGUSR1 属性精确模拟。
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)

    class FakeRuntimeLogger:
        """Accept lifecycle events without writing test logs."""

        def __init__(self, log_directory: Path) -> None:
            """Keep the production constructor boundary."""

            # 日志目录只用于验证构造调用，不需要创建。
            self.log_directory = log_directory

        def emit(self, **event) -> None:
            """Accept one safe lifecycle event."""

            # event 保持可访问，避免测试替身改变调用签名。
            self.last_event = event

    def wait_for_control_request(*args, **kwargs) -> None:
        """Give the real polling thread a deterministic bounded hand-off window."""

        # 截止时间限制失败时的测试时长。
        deadline = time.monotonic() + 1.0
        while control_files.shutdown_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

    monkeypatch.setattr(scheduler_module, "RuntimeLogger", FakeRuntimeLogger)
    monkeypatch.setattr(
        scheduler_module,
        "reconcile_scheduler_once",
        wait_for_control_request,
    )

    # 停止请求在创建 APScheduler 前生效，因此配置替身无需业务字段。
    assert scheduler_module._run_scheduler_unlocked(SimpleNamespace()) == 0


def test_gui_interrupts_owned_scheduler_through_control_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the GUI abort action independent from process signals."""

    # 控制文件替身与 Scheduler 使用同一生产实现。
    control_files = SchedulerControlFiles(tmp_path, "d" * 32)
    # 窗口替身只提供中止分支读取的状态。
    window = SimpleNamespace(
        collecting=False,
        scheduler_job_active=True,
        owned_scheduler=True,
        scheduler_control=control_files,
    )
    # 用户确认固定为 Yes，测试不显示真实弹窗。
    monkeypatch.setattr(
        gui_module.QMessageBox,
        "question",
        lambda *args, **kwargs: gui_module.QMessageBox.Yes,
    )
    # 再次模拟 Windows，确保 GUI 没有隐藏的 SIGUSR1 依赖。
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)

    CollectorWindow.abort_current_collection(window)  # type: ignore[arg-type]

    assert control_files.consume_interruption() is True
