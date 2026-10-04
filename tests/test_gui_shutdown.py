"""Verify a confirmed GUI exit consistently stops only its owned work."""

from types import SimpleNamespace

import pytest

from compass_collector import gui
from compass_collector.run_control import CollectionControl


def test_confirmed_close_does_not_prompt_again_while_waiting(monkeypatch):
    """Repeated close clicks must not reopen a confirmation during resource cleanup."""
    # 用户已授权窗口退出，后续关闭只等待本窗口正在收尾的任务。
    window = SimpleNamespace(pending_close=True, collecting=True, owned_scheduler=False)
    # 捕获关闭事件的结果，不启动Qt应用或其他平台进程。
    outcomes = []
    event = SimpleNamespace(ignore=lambda: outcomes.append("ignored"), accept=lambda: outcomes.append("accepted"))

    def unexpected_prompt(*args):
        """Reject any second confirmation after the exit request was accepted."""
        raise AssertionError("GUI asked for exit confirmation again")

    monkeypatch.setattr(gui.QMessageBox, "question", unexpected_prompt)
    gui.CollectorWindow.closeEvent(window, event)
    assert outcomes == ["ignored"]


def test_exit_stops_collection_and_owned_scheduler_together(monkeypatch):
    """A window owning both responsibilities must not wait for a scheduler it never stopped."""
    # 同一窗口拥有手动工作线程及Scheduler时，一次退出授权必须覆盖两者。
    stopped = []
    control = CollectionControl()
    window = SimpleNamespace(pending_close=False, collecting=True, owned_scheduler=True,
                             collection_control=control,
                             scheduler_control=SimpleNamespace(request_shutdown=lambda: stopped.append("scheduler")),
                             run_status_label=SimpleNamespace(setText=lambda value: None),
                             scheduler_status_label=SimpleNamespace(setText=lambda value: None))
    event = SimpleNamespace(ignore=lambda: stopped.append("ignored"), accept=lambda: stopped.append("accepted"))
    monkeypatch.setattr(gui.QMessageBox, "question", lambda *args: gui.QMessageBox.Yes)
    gui.CollectorWindow.closeEvent(window, event)
    assert control.stop_requested()
    assert control._browser_close_event.is_set()
    assert window.pending_close
    assert stopped == ["scheduler", "ignored"]


@pytest.mark.parametrize("pending_close", [False, True])
def test_idle_or_completed_exit_accepts_close(pending_close):
    """A window with no owned work must accept the initial or deferred close event."""
    # 已完成资源清理后不再依赖第二次用户点击。
    window = SimpleNamespace(pending_close=pending_close, collecting=False, owned_scheduler=False)
    outcomes = []
    event = SimpleNamespace(ignore=lambda: outcomes.append("ignored"), accept=lambda: outcomes.append("accepted"))
    gui.CollectorWindow.closeEvent(window, event)
    assert outcomes == ["accepted"]


def test_declined_exit_keeps_own_collection_running(monkeypatch):
    """Retain the existing cancel-exit behavior without sending a stop request."""
    # 用户拒绝退出时，不能预先中止采集或清理浏览器。
    control = CollectionControl()
    window = SimpleNamespace(pending_close=False, collecting=True, owned_scheduler=False,
                             collection_control=control)
    outcomes = []
    event = SimpleNamespace(ignore=lambda: outcomes.append("ignored"), accept=lambda: outcomes.append("accepted"))
    monkeypatch.setattr(gui.QMessageBox, "question", lambda *args: gui.QMessageBox.No)
    gui.CollectorWindow.closeEvent(window, event)
    assert not control.stop_requested() and not window.pending_close
    assert outcomes == ["ignored"]


def test_late_inspection_event_does_not_reopen_manual_wait_during_exit():
    """Keep a confirmed close active when the worker reports its retained browser."""
    # 中止终态日志晚于退出点击到达时，继续关闭Chrome而非恢复检查按钮。
    control = CollectionControl()
    labels = []
    window = SimpleNamespace(pending_close=True, collection_control=control, collecting=True,
                             inspection_ready=False, progress_state=gui.GuiProgressState(),
                             _append_event=lambda event: None, _apply_notification_event=lambda event: False,
                             _apply_progress_state=lambda: None, _update_action_states=lambda: None,
                             run_status_label=SimpleNamespace(setText=labels.append))
    gui.CollectorWindow.handle_event(window, {"event": "manual_inspection_ready"})
    assert not window.inspection_ready
    assert control._browser_close_event.is_set()
    assert labels == ["正在关闭 Chrome"]
