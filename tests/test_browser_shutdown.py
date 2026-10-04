"""Verify Chrome cleanup does not wait indefinitely for a context close event."""

import os
from pathlib import Path
from threading import Event
from time import time
from time import monotonic
from types import SimpleNamespace

import pytest

from compass_collector.browser import BrowserSession
from compass_collector.browser import open_browser
from compass_collector.config import load_config


def test_lost_context_close_notification_does_not_block_driver_shutdown():
    """The owning driver can close Chrome even when its context notification is lost."""
    # 模拟Chrome已关闭但context事件未到达；stop仍可结束自有驱动。
    closed = Event()

    def wait_for_missing_context_event():
        """Bound the fixture so the old blocking behavior yields a fast failure."""
        closed.wait(0.5)

    # 驱动停止代表其拥有的浏览器资源关闭，不调用其他平台的驱动。
    session = BrowserSession(SimpleNamespace(stop=closed.set),
                             SimpleNamespace(close=wait_for_missing_context_event), None)
    # 旧实现必须先等context通知，无法及时到达驱动停止步骤。
    started = monotonic()
    session.close()
    assert closed.is_set()
    assert monotonic() - started < 0.25


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Requires local Chrome")
def test_real_driver_shutdown_survives_lost_close_event_and_preserves_profile(tmp_path):
    """Close real Chrome after a lost SDK event, then reuse persistent and session cookies."""
    # 全部资源在临时Profile，没有真实账号或网络访问。
    browser = load_config(Path("config/tasks.yaml")).browser_for("compass").model_copy(update={
        "headless": True, "profile_dir": tmp_path / "profile", "persist_session_cookies": True,
    })
    # 保存两种Cookie，验证驱动正常退出后原生Profile及会话备份均保留。
    session = open_browser(browser)
    try:
        session.context.add_cookies([
            {"name": "persistent-probe", "value": "preserved", "domain": "collector.invalid",
             "path": "/", "expires": time() + 3600},
            {"name": "session-probe", "value": "preserved", "domain": "collector.invalid", "path": "/"},
        ])
        # 仅测试注入：模拟Chrome退出时SDK漏收context关闭通知。
        session.context._impl_obj._channel.remove_all_listeners("close")
    finally:
        session.close()
    # 立即重开成功也证明旧Chrome已释放Profile，而非只隐藏GUI或释放业务锁。
    reopened = open_browser(browser)
    try:
        assert {cookie["name"] for cookie in reopened.context.cookies()} >= {"persistent-probe", "session-probe"}
    finally:
        reopened.close()
