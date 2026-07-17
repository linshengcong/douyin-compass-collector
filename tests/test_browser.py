"""Browser metadata validation and safe diagnostic conversion tests."""

import pytest

from compass_collector.browser import BrowserSession
from compass_collector.errors import BrowserOperationError


class FakePage:
    """Return invalid browser metadata while exposing safe diagnostic methods."""

    def evaluate(self, expression: str) -> None:
        """Return no User-Agent for the fixed navigator expression."""

        assert expression == "() => navigator.userAgent"
        return None

    def screenshot(self, *, type: str) -> bytes:
        """Return deterministic PNG bytes for the converted browser error."""

        assert type == "png"
        return b"\x89PNG\r\n\x1a\ninvalid-user-agent"

    def title(self) -> str:
        """Return one safe visible page title."""

        return "电商罗盘"


def test_invalid_user_agent_becomes_a_safe_browser_operation_error() -> None:
    """Keep invalid page metadata inside the browser diagnostic lifecycle."""

    # session 只调用 page.user-agent 边界，不需要真实 Playwright 或 Context。
    session = BrowserSession(
        playwright=object(),  # type: ignore[arg-type]
        context=object(),  # type: ignore[arg-type]
        page=FakePage(),  # type: ignore[arg-type]
    )

    with pytest.raises(BrowserOperationError) as error_info:
        session.user_agent()

    assert error_info.value.category == "browser_page_error"
    assert error_info.value.failed_step == "read_user_agent"
    assert error_info.value.exception_type == "ValueError"
    assert error_info.value.page_title == "电商罗盘"
    assert error_info.value.screenshot is not None
