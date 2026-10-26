"""Local SQLite storage and explicit PostgreSQL compatibility connections."""

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import String, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.types import TypeDecorator


class ExactDecimalText(TypeDecorator):
    """Preserve NUMERIC(24,4) values exactly in SQLite without binary floats."""

    # 固定宽度小数文本保留精度，同时让非负指标的区间约束按数值顺序比较。
    impl = String(25)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Normalize to the same four decimal places as the PostgreSQL schema."""
        if value is None:
            return None
        # 先按十进制转换，避免经过 float 丢失历史金额或计数精度。
        number = Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if not number.is_finite() or abs(number) >= Decimal("1e20"):
            raise ValueError("metric exceeds NUMERIC(24,4)")
        return format(number, "025.4f")

    def process_result_value(self, value, dialect):
        """Expose the same Decimal values to collectors and API payload builders."""
        return Decimal(value) if value is not None else None


def database_url(value: str | URL) -> URL:
    """Accept explicit local SQLite files or PostgreSQL, hiding invalid secrets."""
    try:
        # 只接受显式 URL；不会隐式打开历史 SQLite 归档。
        parsed = make_url(value)
    except Exception:
        raise ValueError("database requires a SQLite or PostgreSQL connection URL") from None
    if parsed.drivername in {"sqlite", "sqlite+pysqlite"}:
        if parsed.host or parsed.username or parsed.password or parsed.query or not parsed.database or parsed.database == ":memory:":
            raise ValueError("SQLite requires an explicit local database file")
        from compass_collector.app_paths import is_packaged_application, portable_data_root
        # 桌面打包版的相对数据库路径落在用户数据目录，避免写入安装目录。
        path = Path(parsed.database).expanduser()
        if not path.is_absolute() and is_packaged_application():
            path = portable_data_root() / path
        return parsed.set(drivername="sqlite+pysqlite", database=str(path.resolve()))
    if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("only SQLite and PostgreSQL are supported")
    if not parsed.host or not parsed.database:
        raise ValueError("PostgreSQL URL requires host and database")
    return parsed.set(drivername="postgresql+psycopg")


def database_engine(value: str | URL):
    """Create durable local storage or an explicitly configured PostgreSQL pool."""
    # 解析后的方言决定连接参数；SQLite 不导入或访问 PostgreSQL 驱动。
    url = database_url(value)
    if url.get_backend_name() == "sqlite":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        # 有界本地连接池供 GUI 和采集线程共用，数据库本身不需要后台服务。
        engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False}, hide_parameters=True)

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, record):
            """Enable foreign keys, durable WAL commits and explicit transactions."""
            connection.isolation_level = None
            # 显式游标确保设置完成后释放资源。
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def begin_sqlite(connection):
            """Serialize short local transactions before reading mutable progress."""
            # 先取得写锁，避免 GUI/调度与采集在读后升级写锁时产生 SQLITE_BUSY。
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(
        url, pool_pre_ping=True, pool_size=3, max_overflow=2,
        pool_timeout=10, pool_recycle=1800, connect_args={"connect_timeout": 10},
        hide_parameters=True,
    )
