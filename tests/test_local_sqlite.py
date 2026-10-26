"""Offline storage, exact historical migration and retryable final HTTP sync."""

import gzip
import json
import socket
import runpy
import sys
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, NoResultFound

from compass_collector.database_connection import database_engine
from compass_collector.local_migration import copy_to_sqlite
from compass_collector.persistence import Database, ProductRankEntryModel, upgrade_database
from compass_collector.ranking_sync import build_sync_payload, sync_published_index
from test_stage_four_persistence import prepare_collected_batch, prepare_staged_csv, PUBLISHED_AT


def test_migration_preserves_sequence_of_an_emptied_table(tmp_path):
    """An empty table still retains IDs consumed by earlier deleted records."""
    # 无剩余商品行的场景无法通过最大现存 ID 恢复历史水位。
    source = URL.create("sqlite", database=str(tmp_path / "source.sqlite3"))
    upgrade_database(source, platform="compass")
    engine = database_engine(source)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO sqlite_sequence(name,seq) VALUES ('product_rank_entries',500)"))
        copy_to_sqlite(source, tmp_path / "destination.sqlite3", platform="compass")
    finally:
        engine.dispose()
    # 目标空表应从历史水位继续分配，不重新使用已删除记录的 ID。
    target = database_engine(URL.create("sqlite", database=str(tmp_path / "destination.sqlite3")))
    try:
        with target.connect() as connection:
            assert connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name='product_rank_entries'")).scalar_one() == 500
    finally:
        target.dispose()


@pytest.mark.parametrize("wrong_platform", [False, True])
def test_cutover_changes_configuration_only_after_all_databases_verify(tmp_path, monkeypatch, wrong_platform):
    """A later database failure must leave the original environment untouched."""
    from dotenv import dotenv_values
    # 三个最小真实源库使配置切换也经过正式迁移入口与全部运行锁。
    variables = (("compass", "COMPASS_DATABASE_URL"), ("taobao", "TAOBAO_DATABASE_URL"), ("taobao", "TAOBAO_POC_DATABASE_URL"))
    lines = ["OTHER_SETTING=preserve-this-value"]
    for index, (platform, variable) in enumerate(variables):
        monkeypatch.delenv(variable, raising=False)
        source = URL.create("sqlite", database=str(tmp_path / f"source-{index}.sqlite3"))
        upgrade_database(source, platform="compass" if wrong_platform and index == 2 else platform)
        lines.append(variable + "=" + source.render_as_string(hide_password=False))
    # 保留原始字节，用于验证失败时不发生任何部分切换。
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\n")
    original = env_file.read_bytes()
    monkeypatch.setattr(sys, "argv", ["migrate", "--apply", "--env-file", str(env_file), "--runtime-root", str(tmp_path / "runtime")])
    if wrong_platform:
        with pytest.raises(ValueError, match="another platform"):
            runpy.run_path("scripts/migrate_collector_to_sqlite.py", run_name="__main__")
        assert env_file.read_bytes() == original
    else:
        runpy.run_path("scripts/migrate_collector_to_sqlite.py", run_name="__main__")
        # 每个平台都切换到新的已核验文件，其他配置和备份保持原样。
        values = dotenv_values(env_file)
        assert values["OTHER_SETTING"] == "preserve-this-value"
        assert all("runtime/local/" in values[variable] for _, variable in variables)
        backup = next((tmp_path / "runtime/backups").glob("sqlite-cutover-*/source.env"))
        assert backup.read_bytes() == original
        report = json.loads((backup.parent / "verification.json").read_text())
        assert report["switched"] and all(item["verified"] for item in report["databases"].values())


def test_sqlite_schema_and_transactions_require_no_network(tmp_path, monkeypatch):
    """Exercise real migrations and rollback with socket connections prohibited."""
    def deny_network(*args, **kwargs):
        """Any hidden database service dependency must fail this offline test."""
        raise AssertionError("local persistence must not connect to a network")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    # 显式新文件与生产迁移入口覆盖首次建库和重复启动。
    url = URL.create("sqlite", database=str(tmp_path / "local.sqlite3"))
    upgrade_database(url, platform="compass")
    upgrade_database(url, platform="compass")
    engine = database_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "sq0001_local"
            assert "WHERE platform = 'compass'" in connection.execute(text("SELECT sql FROM sqlite_master WHERE name='uq_category_run_compass_product'")).scalar_one()
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("INSERT INTO raw_responses(category_run_id,page_no,path,item_count,captured_at,safe_params) VALUES ('absent',1,'test',0,'2026-10-12','{}')"))
        with pytest.raises(ValueError, match="another platform"):
            upgrade_database(url, platform="taobao")
    finally:
        engine.dispose()


def test_migration_preserves_exact_metrics_ids_and_sync_payload(tmp_path, monkeypatch):
    """A complete historical batch must retain every field and API identity."""
    # 本测试检查 SQLite 自增水位，始终使用本地文件，不受可选 PG 测试开关影响。
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    # 使用真实分类、分页和发布事务建立源库，覆盖所有业务表的关联。
    database, batch = prepare_collected_batch(tmp_path / "source", mode="normal", category_statuses=("success",))
    destination = tmp_path / "destination.sqlite3"
    try:
        database.publish_collected_batch(batch, 1, prepare_staged_csv(tmp_path / "source"), PUBLISHED_AT)
        # 超过二进制浮点精度的 NUMERIC(24,4) 必须逐位保留。
        minimum = Decimal("12345678901234567890.1234")
        maximum = Decimal("12345678901234567890.1235")
        with database.engine.begin() as connection:
            connection.execute(update(ProductRankEntryModel).values(pay_amount_min_value=minimum, pay_amount_max_value=maximum))
            # 自增水位可以高于现存最大 ID，迁移不能重新使用曾经消耗的身份。
            connection.execute(text("UPDATE sqlite_sequence SET seq=1000000 WHERE name='product_rank_entries'"))
        monkeypatch.setenv("COMPASS_DATABASE_URL", database.engine.url.render_as_string(hide_password=False))
        # 同一个批次索引用于迁移前后精确比对，不依赖当前配置重建历史。
        index = {"platform": "compass", "task_id": batch.task_id, "batch_id": batch.batch_id,
                 "business_date": batch.business_date.isoformat(), "item_count": 1, "shops_schema_version": 1}
        original = build_sync_payload(index)
        report = copy_to_sqlite(database.engine.url, destination, platform="compass")
        assert report["verified"] and report["tables"]["product_rank_entries"]["rows"] == 1
        # 新连接核对水位，防止只复制现存行导致后续 ID 回退。
        migrated_engine = database_engine(URL.create("sqlite", database=str(destination)))
        try:
            with migrated_engine.connect() as connection:
                assert connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name='product_rank_entries'")).scalar_one() == 1000000
        finally:
            migrated_engine.dispose()
        monkeypatch.setenv("COMPASS_DATABASE_URL", URL.create("sqlite", database=str(destination)).render_as_string(hide_password=False))
        assert build_sync_payload(index) == original
        assert original["rows"][0]["metrics"]["pay_amount_min_value"] == str(minimum)
        # 同一目标绝不能再次导入或覆盖；原文件保持不变。
        with pytest.raises(FileExistsError):
            copy_to_sqlite(database.engine.url, destination, platform="compass")
        # 错误平台应在建目标文件前拒绝，不生成似乎可用的空库。
        with pytest.raises(ValueError, match="another platform"):
            copy_to_sqlite(database.engine.url, tmp_path / "wrong.sqlite3", platform="taobao")
        assert not (tmp_path / "wrong.sqlite3").exists()
    finally:
        database.close()


def test_failed_final_sync_retries_same_published_batch_without_recollection(tmp_path, monkeypatch):
    """Only a published batch is uploaded; network failure preserves local success."""
    # 本地数据库、raw 和 CSV 均通过生产 fixture 实际落盘。
    database, batch = prepare_collected_batch(tmp_path, mode="normal", category_statuses=("success",))
    index = {"platform": "compass", "task_id": batch.task_id, "batch_id": batch.batch_id,
             "business_date": batch.business_date.isoformat(), "item_count": 1, "shops_schema_version": 1}
    index_path = tmp_path / "latest.json"
    index_path.write_text(json.dumps(index))
    monkeypatch.setenv("COMPASS_DATABASE_URL", database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("RANKING_API_URL", "https://ranking.example.test")
    monkeypatch.setenv("RANKING_SYNC_TOKEN", "synthetic-test-token")
    # 替换 HTTP 传输层，实际执行 payload 读取、压缩、鉴权和回执逻辑。
    requests = []
    client_class = httpx.Client

    def receive(request):
        """First upload fails, second acknowledges exactly the same batch."""
        requests.append(json.loads(gzip.decompress(request.content)))
        assert request.url.path == "/api/rankings/sync"
        assert request.headers["authorization"] == "Bearer synthetic-test-token"
        return httpx.Response(503 if len(requests) == 1 else 200, json={"batch_id": batch.batch_id})

    monkeypatch.setattr("compass_collector.ranking_sync.httpx.Client", lambda **kwargs: client_class(transport=httpx.MockTransport(receive), **kwargs))
    try:
        # 采集中尚未 published_at，不能上传半成品。
        with pytest.raises(NoResultFound):
            sync_published_index(index_path)
        assert requests == []
        database.publish_collected_batch(batch, 1, prepare_staged_csv(tmp_path), PUBLISHED_AT)
        with pytest.raises(ValueError, match="ranking_sync_http_503"):
            sync_published_index(index_path)
        assert database.collection_snapshot(batch.batch_id).status == "success"
        assert not (tmp_path / "ranking-sync.json").exists()
        assert sync_published_index(index_path) == {"batch_id": batch.batch_id}
        assert requests[0] == requests[1]
        assert (tmp_path / "ranking-sync.json").is_file()
    finally:
        database.close()
