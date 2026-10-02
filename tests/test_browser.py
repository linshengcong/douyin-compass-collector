"""Browser diagnostics must exclude URLs and authentication material."""

import json
from types import SimpleNamespace

import pytest

from compass_collector.browser import (
    BrowserSession, build_page_error, restore_session_cookies, save_session_cookies,
)
from compass_collector.errors import BrowserOperationError


def test_page_error_retains_only_safe_diagnostics():
    """Keep local screenshot without copying sensitive exception text."""

    # Lightweight page exercises the public diagnostic boundary.
    class FakePage:
        def screenshot(self, **kwargs):
            """Return deterministic local pixels."""
            return b"png"

    error = build_page_error(
        FakePage(),
        ValueError("https://secret.invalid/?token=secret"),
        failed_step="navigate",
    )
    assert error.screenshot == b"png"
    assert error.exception_type == "ValueError"
    assert error.safe_page_path is None
    assert "secret" not in str(error)


def test_restore_does_not_replace_native_cookie_and_rejects_corrupt_snapshot(tmp_path):
    """Native cookies take priority and corrupt credentials never appear in errors."""
    # 只使用合成凭证，验证真实 Cookie 身份键和损坏文件边界。
    cookie = {"name": "auth", "value": "synthetic-secret", "domain": "example.invalid", "path": "/", "expires": -1}
    path = tmp_path / ".session-cookies.json"
    path.write_text(json.dumps({"version": 1, "cookies": [cookie]}))
    restored = []
    context = SimpleNamespace(cookies=lambda: [dict(cookie, expires=9999999999)],
                              add_cookies=lambda cookies: restored.extend(cookies))
    restore_session_cookies(context, path)
    assert restored == []
    path.write_text('{"synthetic-secret": invalid}')
    with pytest.raises(BrowserOperationError) as caught:
        restore_session_cookies(context, path)
    assert caught.value.failed_step == "restore_session_cookies"
    assert "synthetic-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_failed_atomic_save_preserves_snapshot_and_cleans_temporary_file(tmp_path, monkeypatch):
    """A failed replace must keep the previous complete file without leaving secrets behind."""
    # 注入替换失败，验证不会截断原始备份或遗留临时凭证文件。
    path = tmp_path / ".session-cookies.json"
    path.write_text("previous-complete-snapshot")

    def reject_replace(*args):
        """Simulate a filesystem failure after the temporary file has been written."""
        raise OSError("synthetic failure")

    monkeypatch.setattr("compass_collector.browser.os.replace", reject_replace)
    with pytest.raises(BrowserOperationError):
        save_session_cookies(SimpleNamespace(cookies=lambda: []), path)
    assert path.read_text() == "previous-complete-snapshot"
    assert list(tmp_path.glob(".session-cookies-*")) == []


def test_snapshot_failure_still_closes_context_and_stops_playwright(tmp_path):
    """Credential checkpoint errors must not retain a Chrome process or Profile lock."""
    # close 的错误路径同样执行原有两层资源释放。
    events = []

    def unavailable_cookies():
        """Raise a synthetic storage error before closing the context."""
        raise RuntimeError("synthetic failure")

    session = BrowserSession(
        SimpleNamespace(stop=lambda: events.append("stopped")),
        SimpleNamespace(cookies=unavailable_cookies, close=lambda: events.append("closed")),
        None, tmp_path / ".session-cookies.json",
    )
    with pytest.raises(BrowserOperationError):
        session.close()
    assert events == ["closed", "stopped"]
