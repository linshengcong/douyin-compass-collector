"""Apply the independent local SQLite revision chain."""

from alembic import context
from compass_collector.persistence import Base

# 应用入口提供已通过 BEGIN IMMEDIATE 加锁的连接，迁移不另开数据库。
config = context.config


def run():
    """Run transactional DDL and platform ownership on the caller's connection."""
    # 显式连接由 upgrade_database 创建，避免把 SQLite 路径写进共享 INI。
    connection = config.attributes.get("connection")
    if connection is None or connection.dialect.name != "sqlite":
        raise ValueError("SQLite migrations require the application database entry point")
    context.configure(connection=connection, target_metadata=Base.metadata, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


run()
