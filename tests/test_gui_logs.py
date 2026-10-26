"""Exercise real log widgets across startup bursts, filters and history eviction."""

from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from compass_collector import gui
from compass_collector.config import load_config


@pytest.fixture
def log_window(monkeypatch):
    """Construct the real GUI with database, locks and remote preview isolated."""

    # Qt controls remain real; no collection or database resources are opened.
    application = gui.ensure_qt_application()
    monkeypatch.setattr(gui, "latest_published_csv", lambda *args: None)
    monkeypatch.setattr(gui, "read_latest_batch_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(gui, "lock_is_held", lambda *args: False)
    monkeypatch.setattr(gui.CollectorWindow, "_refresh_rules_summary", lambda self: None)
    # The repository config is parsed only for the existing layout contract.
    config = load_config(Path("config/taobao.yaml")).for_platform("taobao")
    window = gui.CollectorWindow(
        config,
        gui.GuiLaunchRequest(
            Path("config/taobao.yaml"), "taobao_household_cleaning_realtime",
            False, platform="taobao",
        ),
    )
    window.scheduler_timer.stop()
    window.scheduler_event_timer.stop()
    yield window
    window.close()
    application.processEvents()


def log_event(number, level="INFO"):
    """Generate the same safe columns as a discovery event without private data."""

    return {
        "timestamp": "2026-10-09T11:47:57+08:00",
        "level": level,
        "stage": "category_discovery",
        "event": "category_discovered",
        "message": f"分类 {number}",
    }


def wait_for_log_flush():
    """Let Qt deliver the short batch timer while continuing event processing."""

    # A local event loop avoids blocking the GUI thread with sleep.
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()


def test_startup_burst_is_coalesced_and_keeps_existing_cells(log_window, monkeypatch):
    """A real 141-category burst must append once instead of rebuilding history."""

    for number in range(500):
        log_window._append_event(log_event(number), render=False)
    log_window._render_events()
    # Existing cells must keep identity across normal event delivery.
    first_cell = log_window.log_table.item(0, 3)
    log_window.log_table.selectRow(0)
    original_item = gui.QTableWidgetItem
    created_cells = []

    def count_item(value):
        """Count allocations at the actual Qt item construction boundary."""

        created_cells.append(value)
        return original_item(value)

    monkeypatch.setattr(gui, "QTableWidgetItem", count_item)
    for number in range(500, 641):
        log_window.handle_event(log_event(number))
    assert created_cells == []
    wait_for_log_flush()
    assert len(created_cells) == 141 * 4
    assert log_window.log_table.rowCount() == 641
    assert log_window.log_table.item(0, 3) is first_cell
    assert log_window.log_table.item(640, 3).text() == "分类 640"
    # Appending logs preserves the selected row and the existing copy behavior.
    log_window._copy_selected_logs()
    assert gui.QApplication.clipboard().text() == "11:47:57\tINFO\tcategory_discovery\t分类 0"


def test_filter_change_consumes_pending_events_without_duplicates(log_window):
    """Switching level rebuilds immediately and cancels the stale pending flush."""

    log_window._append_event(log_event(1, "INFO"))
    log_window._append_event(log_event(2, "ERROR"))
    log_window.level_filter.setCurrentText("ERROR")
    assert log_window.log_table.rowCount() == 1
    assert log_window.log_table.item(0, 3).text() == "分类 2"
    wait_for_log_flush()
    assert log_window.log_table.rowCount() == 1
    log_window._append_event(log_event(3, "INFO"))
    log_window._append_event(log_event(4, "ERROR"))
    wait_for_log_flush()
    assert log_window.log_table.rowCount() == 2
    log_window.level_filter.setCurrentText("全部")
    assert [log_window.log_table.item(row, 3).text() for row in range(4)] == [
        "分类 1", "分类 2", "分类 3", "分类 4",
    ]


def test_history_eviction_removes_only_expired_filtered_rows(log_window, monkeypatch):
    """The cap applies to all events while preserving surviving filtered cells."""

    monkeypatch.setattr(gui, "MAX_VISIBLE_EVENTS", 4)
    for number, level in ((1, "ERROR"), (2, "INFO"), (3, "ERROR"), (4, "INFO")):
        log_window._append_event(log_event(number, level), render=False)
    log_window.level_filter.setCurrentText("ERROR")
    # The second visible row survives eviction of the first two historical events.
    surviving_cell = log_window.log_table.item(1, 3)
    log_window._append_event(log_event(5, "ERROR"))
    log_window._append_event(log_event(6, "INFO"))
    wait_for_log_flush()
    assert [event["message"] for event in log_window.events] == [
        "分类 3", "分类 4", "分类 5", "分类 6",
    ]
    assert log_window.log_table.rowCount() == 2
    assert log_window.log_table.item(0, 3) is surviving_cell
    assert log_window.log_table.item(1, 3).text() == "分类 5"


def test_clear_history_cancels_pending_rows_and_allows_new_logs(log_window):
    """The existing cleanup reset must never restore queued old rows."""

    log_window._append_event(log_event(1))
    log_window.events.clear()
    log_window._render_events()
    wait_for_log_flush()
    assert log_window.log_table.rowCount() == 0
    log_window._append_event(log_event(2))
    wait_for_log_flush()
    assert log_window.log_table.rowCount() == 1
    assert log_window.log_table.item(0, 3).text() == "分类 2"


def test_pending_burst_larger_than_history_cap_only_renders_retained_rows(log_window, monkeypatch):
    """A delayed timer may discard unseen rows without retaining their Qt items."""

    monkeypatch.setattr(gui, "MAX_VISIBLE_EVENTS", 4)
    for number in range(10):
        log_window._append_event(log_event(number))
    wait_for_log_flush()
    assert log_window.log_table.rowCount() == 4
    assert [log_window.log_table.item(row, 3).text() for row in range(4)] == [
        "分类 6", "分类 7", "分类 8", "分类 9",
    ]


def test_business_progress_updates_before_batched_log_display(log_window):
    """Only the log table waits for the timer; discovery progress stays immediate."""

    log_window.handle_event({
        "event": "category_discovery_succeeded",
        "level": "INFO",
        "message": "已发现 141 个分类",
        "discovered_category_count": 141,
    })
    assert log_window.progress_state.category_total == 141
    assert log_window.progress_bar.maximum() == 141
    assert log_window.log_table.rowCount() == 0
    wait_for_log_flush()
    assert log_window.log_table.rowCount() == 1
