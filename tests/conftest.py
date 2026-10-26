"""Default SQLite integration environment, never using production configuration."""

import pytest
from pg_support import pg_url, close_test_schemas


@pytest.fixture(autouse=True)
def isolated_database_environment(request, monkeypatch):
    """Route default configurations to isolated SQLite files or optional PG schemas."""
    # 每个平台和每个测试均隔离；默认 SQLite 不需要 Docker 或网络。
    for platform, variable in (("compass", "COMPASS_DATABASE_URL"), ("taobao", "TAOBAO_DATABASE_URL"), ("taobao_poc", "TAOBAO_POC_DATABASE_URL")):
        monkeypatch.setenv(variable, pg_url(f"{request.node.nodeid}/{platform}").render_as_string(hide_password=False))


def pytest_sessionfinish(session, exitstatus):
    """Release ephemeral schemas after all test connections have completed."""
    close_test_schemas()
