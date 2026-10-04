"""Run PostgreSQL migrations on a caller-owned transaction or explicit DSN."""

import os
from alembic import context
from compass_collector.database_connection import database_engine, database_url
from compass_collector.persistence import Base

# 模型只用于差异检测，版本内的建表定义保持独立固定。
config = context.config
target_metadata = Base.metadata


def migrate(connection) -> None:
    """Apply revisions to an existing PostgreSQL connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    """Resolve credentials without interpolating them into Alembic INI settings."""
    # 应用入口提供已加锁的事务连接；命令行迁移自行创建连接。
    connection = config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
        return
    # 独立 alembic 命令使用 DATABASE_URL，支持百分号密码和 SSL 参数。
    value = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if context.is_offline_mode():
        context.configure(url=database_url(value), target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    # 连接和平台初始化通过生产入口完成；CLI 迁移也持有同一个数据库锁。
    engine = database_engine(value)
    try:
        from sqlalchemy import text
        with engine.begin() as active_connection:
            active_connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            active_connection.execute(text("SELECT pg_advisory_xact_lock(172936, 1)"))
            migrate(active_connection)
    finally:
        engine.dispose()


run()
