"""PostgreSQL connection contract, native schema and archive isolation."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import inspect, text
from compass_collector.config import DatabaseConfig
from compass_collector.database_connection import database_url
from compass_collector.persistence import Database, upgrade_database, normalize_datetime
from pg_support import pg_url


@pytest.mark.parametrize("value", ["sqlite:///old.db", Path("old.db"), "mysql://user:secret@host/db", "not-a-url-secret"])
def test_only_postgresql_urls_are_accepted_without_secret_echo(value):
    """Reject old paths and unsupported drivers with a safe error."""
    with pytest.raises(ValueError) as result:
        database_url(value)
    assert "secret" not in str(result.value)


def test_dsn_preserves_encoded_password_and_rds_tls():
    """RDS TLS query parameters and escaped credentials survive normalization."""
    # 合成密码验证 DSN 不经过 INI 百分号插值。
    url = database_url("postgresql://user:p%25%40ss@host/db?sslmode=verify-full&sslrootcert=/tmp/root.crt")
    assert url.password == "p%@ss"
    assert url.drivername == "postgresql+psycopg"
    assert url.query["sslmode"] == "verify-full"


def test_missing_database_environment_fails_without_fallback(monkeypatch):
    """An explicit connection is required; old SQLite is never a fallback."""
    monkeypatch.delenv("MISSING_TEST_PG_URL", raising=False)
    with pytest.raises(ValueError, match="is required"):
        DatabaseConfig(url_env="MISSING_TEST_PG_URL").url


def test_timezone_conversion_keeps_beijing_business_time():
    """Convert aware timestamps before storing Beijing wall-clock values."""
    assert normalize_datetime(datetime(2026, 10, 3, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 3, 8)


def test_pg_baseline_has_native_partial_index_and_exact_numeric(tmp_path):
    """Verify PG's real index predicate and numeric precision after migration."""
    # 实际数据库检查防止 SQLite 方言参数在 PG 中被忽略。
    url = pg_url(tmp_path)
    upgrade_database(url, platform="compass")
    database = Database(url)
    try:
        indexes = inspect(database.engine).get_indexes("product_rank_entries")
        index = next(item for item in indexes if item["name"] == "uq_category_run_compass_product")
        assert index["unique"]
        assert "compass" in index["dialect_options"]["postgresql_where"]
        with database.engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "pg0001_initial"
            assert connection.execute(text("SELECT numeric_precision,numeric_scale FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='product_rank_entries' AND column_name='pay_amount_min_value'")).one() == (24, 4)
    finally:
        database.close()
