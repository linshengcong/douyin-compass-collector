"""Verified, non-destructive migration from a collector database to local SQLite."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import URL

from compass_collector.database_connection import database_engine
from compass_collector.persistence import Base, upgrade_database


def _canonical_row(row):
    """Compare logical values across dialects without converting exact decimals."""
    # 标准化仅用于摘要，不修改数据库内容或原始材料。
    values = dict(row)
    for key, value in values.items():
        if isinstance(value, (date, datetime)):
            values[key] = value.isoformat()
        elif isinstance(value, Decimal):
            values[key] = format(value, ".4f")
    return (json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _preserve_sequence(connection, table_name, last_value):
    """Retain consumed IDs even when the source table was emptied later."""
    # 空表不会自动建立 sqlite_sequence 记录，先补齐再提高已消耗 ID。
    connection.execute(text("INSERT INTO sqlite_sequence(name, seq) SELECT :name, :value WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name=:name)"), {"name": table_name, "value": last_value})
    connection.execute(text("UPDATE sqlite_sequence SET seq=max(seq, :value) WHERE name=:name"), {"name": table_name, "value": last_value})


def copy_to_sqlite(source_url, destination: Path, *, platform: str, allow_empty=False):
    """Copy one frozen source, verify all rows, and expose only a complete new file."""
    if platform not in {"compass", "taobao"}:
        raise ValueError("unsupported database platform")
    # 严禁覆盖既有 SQLite；失败的临时文件也保留供排查，重试须用新路径。
    destination = destination.resolve()
    pending = destination.with_name(destination.name + ".pending")
    if destination.exists() or pending.exists():
        raise FileExistsError("migration destination already exists")
    # 当前基线只承接已明确支持的 schema，不猜测未知历史版本。
    source = database_engine(source_url)
    target = None
    # 清单只记录摘要和计数，不包含连接密码或业务内容。
    report = {"platform": platform, "destination": str(destination), "tables": {}, "source_versions": []}
    try:
        with source.connect() as connection:
            if source.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            else:
                connection.execute(text("PRAGMA query_only=ON"))
            # 不接受部分建表的源库，避免静默遗漏历史关系。
            tables = set(inspect(connection).get_table_names())
            if tables:
                if tables != set(Base.metadata.tables) | {"alembic_version"}:
                    raise ValueError("source schema does not match the collector schema")
                report["source_versions"] = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
                if report["source_versions"] not in (["pg0002_limits_shops"], ["sq0001_local"]):
                    raise ValueError("source schema revision is unsupported")
                # 同时核验永久归属和历史批次归属，不能混库迁移。
                owners = set(connection.execute(text("SELECT platform FROM runtime_platform UNION SELECT platform FROM collection_batches")).scalars())
                if owners - {platform}:
                    raise ValueError("source database belongs to another platform")
            elif not allow_empty:
                raise ValueError("source database is empty")
            # 先建立独立的本地 schema，迁移版本不从另一后端直接复制。
            target_url = URL.create("sqlite+pysqlite", database=str(pending))
            upgrade_database(target_url)
            pending.chmod(0o600)
            target = database_engine(target_url)
            with target.begin() as local:
                for table in Base.metadata.sorted_tables:
                    # 按父子表顺序保留原始主键、外键和发布时间。
                    digest = hashlib.sha256()
                    count = 0
                    if table.name in tables:
                        # 有界流式读取减少远程往返且避免全部历史驻留内存。
                        result = connection.execution_options(yield_per=5000).execute(select(table).order_by(*table.primary_key.columns))
                        for partition in result.mappings().partitions(5000):
                            # 插入经过同一类型系统，日期、JSON 和精确小数跨方言一致。
                            rows = [dict(row) for row in partition]
                            local.execute(table.insert(), rows)
                            for row in rows:
                                digest.update(_canonical_row(row))
                            count += len(rows)
                            print(f"{platform}: copying {table.name}: {count}", flush=True)
                        result.close()
                    report["tables"][table.name] = {"rows": count, "sha256": digest.hexdigest()}
                    print(f"{platform}: copied {table.name}: {count}", flush=True)
                # 保留已消耗但后来删除的自增 ID，避免重新使用历史来源身份。
                if source.dialect.name == "postgresql":
                    for sequence, last_value in connection.execute(text("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname=current_schema()")):
                        # 标准迁移使用固定的 <table>_id_seq 命名，其他序列不处理。
                        table_name = sequence.removesuffix("_id_seq")
                        if table_name in Base.metadata.tables and last_value is not None:
                            _preserve_sequence(local, table_name, last_value)
                elif tables:
                    for table_name, last_value in connection.execute(text("SELECT name, seq FROM sqlite_sequence")):
                        _preserve_sequence(local, table_name, last_value)
            # 使用新连接重读已提交数据，比对全部字段而不只是行数。
            with target.connect() as local:
                for table in Base.metadata.sorted_tables:
                    digest = hashlib.sha256()
                    count = 0
                    for row in local.execute(select(table).order_by(*table.primary_key.columns)).mappings():
                        digest.update(_canonical_row(row))
                        count += 1
                    if report["tables"][table.name] != {"rows": count, "sha256": digest.hexdigest()}:
                        raise ValueError("migration verification failed for " + table.name)
                if local.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("migrated database has invalid foreign keys")
                if local.exec_driver_sql("PRAGMA integrity_check").scalar_one() != "ok":
                    raise ValueError("migrated database failed integrity check")
            target.dispose()
            # 在原子替换前确保 WAL 已归并，数据库单文件即可完整恢复。
            with closing(sqlite3.connect(pending)) as checkpoint:
                if checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise RuntimeError("migration WAL checkpoint is busy")
            pending.rename(destination)
            report["verified"] = True
            return report
    finally:
        source.dispose()
        if target is not None:
            target.dispose()
