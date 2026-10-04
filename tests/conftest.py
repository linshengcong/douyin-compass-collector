"""Optional PostgreSQL environment for integration tests and safe schema cleanup."""

import os
import pytest
from pg_support import pg_url, close_test_schemas


@pytest.fixture(autouse=True)
def isolated_database_environment(request, monkeypatch):
    """Route default configurations to isolated schemas when PG tests are enabled."""
    if os.environ.get("TEST_DATABASE_URL"):
        for platform, variable in (("compass", "COMPASS_DATABASE_URL"), ("taobao", "TAOBAO_DATABASE_URL"), ("taobao_poc", "TAOBAO_POC_DATABASE_URL")):
            # 不允许真实 GUI 或入口测试读取开发数据库；每个测试隔离。
            monkeypatch.setenv(variable, pg_url(f"{request.node.nodeid}/{platform}").render_as_string(hide_password=False))
    else:
        for platform, variable in (("compass", "COMPASS_DATABASE_URL"), ("taobao", "TAOBAO_DATABASE_URL"), ("taobao_poc", "TAOBAO_POC_DATABASE_URL")):
            # 配置及假数据库测试无需连接；实际存储测试必须显式调用 pg_url。
            monkeypatch.setenv(variable, f"postgresql+psycopg://localhost/{platform}_test")


def pytest_sessionfinish(session, exitstatus):
    """Release ephemeral schemas after all test connections have completed."""
    close_test_schemas()
