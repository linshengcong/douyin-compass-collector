"""Isolated SQLite by default, with optional real PostgreSQL compatibility tests."""

import os
from hashlib import sha256
from uuid import uuid4
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from compass_collector.database_connection import database_url

# 每次 pytest 运行使用不同前缀；只清理本进程创建的 schema。
RUN_PREFIX = "collector_test_" + uuid4().hex[:12]
SCHEMAS: dict[str, str] = {}
# 无 Docker 测试使用本次 pytest 独立目录，测试结束后只清理本次创建的文件。
SQLITE_DIRECTORY = TemporaryDirectory(prefix="collector-sqlite-test-")


def pg_url(key):
    """Map an existing fixture identity to an isolated real database."""
    # 未配置专用 PostgreSQL 测试库时，完整运行本地 SQLite 集成测试。
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        return URL.create("sqlite+pysqlite", database=str(Path(SQLITE_DIRECTORY.name) / (sha256(str(key).encode()).hexdigest() + ".sqlite3")))
    base = database_url(value)
    if not base.database.endswith("_test"):
        raise ValueError("TEST_DATABASE_URL must use a dedicated database ending in _test")
    # 稳定 key 让同一测试重复打开连接仍指向同一个 schema。
    identity = str(key)
    if identity not in SCHEMAS:
        schema = RUN_PREFIX + "_" + sha256(identity.encode()).hexdigest()[:16]
        engine = create_engine(base)
        try:
            with engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        finally:
            engine.dispose()
        SCHEMAS[identity] = schema
    return base.update_query_dict({"options": "-csearch_path=" + SCHEMAS[identity]})


def pg_config(config, key):
    """Give each configured platform a separate schema and secret environment key."""
    # 名称只在测试进程环境中存在，绝不写回项目 .env。
    platforms = {}
    for name, item in config.platforms.items():
        environment_name = "TEST_PG_" + sha256(f"{key}/{name}".encode()).hexdigest()[:20].upper()
        os.environ[environment_name] = pg_url(f"{key}/{name}").render_as_string(hide_password=False)
        platforms[name] = item.model_copy(update={"database_env": environment_name})
    first = next(iter(platforms.values())).database_env
    return config.model_copy(update={"platforms": platforms, "database": config.database.model_copy(update={"url_env": first})})


def close_test_schemas():
    """Remove only the schemas created by this test process."""
    SQLITE_DIRECTORY.cleanup()
    value = os.environ.get("TEST_DATABASE_URL")
    if not value or not SCHEMAS:
        return
    # 复用同一个管理员连接执行本次运行允许的清理。
    engine = create_engine(database_url(value))
    try:
        for schema in SCHEMAS.values():
            # 每个 schema 单独提交，避免大量 DROP 在同一事务耗尽 PG 锁表。
            with engine.begin() as connection:
                if not schema.startswith(RUN_PREFIX + "_"):
                    raise ValueError("refusing to remove an unowned test schema")
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
