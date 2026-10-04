"""PostgreSQL connection validation shared by configuration and persistence."""

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url


def database_url(value: str | URL) -> URL:
    """Accept PostgreSQL only, hiding invalid DSNs from error messages."""
    try:
        # 只解析显式连接地址，旧 SQLite Path 不再作为运行时输入。
        parsed = make_url(value)
    except Exception:
        raise ValueError("database requires a PostgreSQL connection URL") from None
    if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("only PostgreSQL is supported")
    if not parsed.host or not parsed.database:
        raise ValueError("PostgreSQL URL requires host and database")
    return parsed.set(drivername="postgresql+psycopg")


def database_engine(value: str | URL):
    """Create a bounded pool with stale-connection detection for local PG or RDS."""
    return create_engine(
        database_url(value), pool_pre_ping=True, pool_size=3, max_overflow=2,
        pool_timeout=10, pool_recycle=1800, connect_args={"connect_timeout": 10},
        hide_parameters=True,
    )
