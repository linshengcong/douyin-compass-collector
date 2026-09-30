"""Browser diagnostics must exclude URLs and authentication material."""

from compass_collector.browser import build_page_error


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
