"""Platform-aware status and cleanup tests using real isolated SQLite relationships."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from compass_collector.local_data import clear_local_data_with_locks
from compass_collector.models import CategoryRunPlan
from compass_collector.persistence import (
    CategoryRun, CollectionBatch, ProductRankEntryModel, ProductRankEntryShopModel, RawResponse,
)
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy
from compass_collector.platform_runtime import PlatformRuntime
from test_category_batch_persistence import build_discovery, create_database


def create_both_platforms(tmp_path):
    """Create parent/child rows and files for both platforms without browser access."""
    # 正式迁移建立外键约束；所有文件及数据库都处于pytest临时runtime。
    # 同一runtime的独立数据库，不能将两个平台放进同一个文件。
    databases = {}
    # 北京墙上时间仅用于确保淘宝记录更新，验证过滤先于LIMIT。
    started = datetime(2026, 10, 2, 14)
    for index, platform in enumerate(("compass", "taobao")):
        from compass_collector.persistence import Database, upgrade_database
        database_path = tmp_path / "runtime/data" / f"{platform}.db"
        upgrade_database(database_path, platform=platform)
        database = Database(database_path)
        databases[platform] = database
        # 同一平台的批次、任务和分类ID保持独立。
        task_id = f"{platform}_task"
        manifest = tmp_path / "runtime" / "raw" / platform / "2026-10-02" / task_id / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("synthetic")
        database.create_batch(batch_id=platform, task_id=task_id, platform=platform,
                              business_date=started.date(), planned_at=started, mode="force",
                              brand_type=None, price_bin=None, manifest_path=manifest,
                              started_at=started + timedelta(minutes=index))
        # 分类树文件及分类创建都走生产接口；直接ORM只填充级联删除的关联叶节点。
        tree = manifest.parent / "tree.json.gz"
        tree.write_bytes(b"synthetic")
        database.record_category_tree_raw(batch_id=platform, category_tree_raw_path=tree)
        discovery = build_discovery()
        plans = tuple(CategoryRunPlan(category_run_id=f"{platform}-{item.discovery_order}", category=item)
                      for item in discovery.categories)
        database.create_category_runs(batch_id=platform, discovery=discovery, category_run_plans=plans)
        with database.session_factory.begin() as session:
            # 子记录允许检查真实外键级联，不能仅看顶层状态计数。
            entry = ProductRankEntryModel(platform=platform, category_run_id=plans[0].category_run_id,
                                          captured_at=started, page_no=1, rank=1,
                                          product_id="synthetic", product_name="synthetic", newly_on_ranking=False)
            session.add(entry)
            session.flush()
            session.add(ProductRankEntryShopModel(entry_id=entry.id, position=1, shop_name="synthetic"))
            session.add(RawResponse(category_run_id=plans[0].category_run_id, page_no=1,
                                    path=str(tree), item_count=1, captured_at=started))
        database.set_scheduler_checkpoint(task_id, started)
        for name in ("exports", "artifacts", "web-publication"):
            # 两个平台的现代目录，以及只有日期/task维度的历史目录。
            file = tmp_path / "runtime" / name / platform / "data.txt"
            file.parent.mkdir(parents=True)
            file.write_text("synthetic")
        legacy = tmp_path / "runtime" / "exports" / "2026-10-02" / task_id / "old.csv"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("synthetic")
        # 旧日期/task布局需要可核验的平台来源。
        legacy_manifest = tmp_path / "runtime/raw/2026-10-02" / task_id / platform / "manifest.json"
        legacy_manifest.parent.mkdir(parents=True)
        legacy_manifest.write_text(json.dumps({"platform": platform}))
    return databases


@pytest.mark.parametrize("platform", ["compass", "taobao"])
def test_platform_cleanup_preserves_other_platform_rows_files_profiles_and_logs(tmp_path, platform):
    """Clear selected rows with real foreign keys while retaining unrelated historical data."""
    # 所选平台及另一平台共用数据库，覆盖最容易误清理的配置形式。
    databases = create_both_platforms(tmp_path)
    assert databases[platform].recent_status(limit=1, platform=platform)[0].batch_id == platform
    for database in databases.values():
        database.close()
    # Profile、共享日志和未知文件不属于平台清理白名单。
    preserved = [tmp_path / "runtime" / "logs" / "2026-10-02.jsonl",
                 tmp_path / "runtime" / "browser-profile" / "Login Data",
                 tmp_path / "runtime" / "taobao-browser-profile" / "Login Data",
                 tmp_path / "runtime" / "custom.txt"]
    for file in preserved:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("preserve")
    # 调用真实加锁入口，SQLite文件应保留，所选批次被级联清除。
    # 另一平台同时调度及采集时，仍可清理自己的独立数据库与材料。
    other = "taobao" if platform == "compass" else "compass"
    other_scope = PlatformRuntime(tmp_path / "runtime", other)
    with other_scope.operation("scheduler"), other_scope.operation("collection"):
        summary = clear_local_data_with_locks(tmp_path / "runtime", tmp_path / "runtime/data" / f"{platform}.db",
                                              platform=platform, task_ids=(f"{platform}_task",))
    assert summary.succeeded and summary.database_batches == 1 and summary.database_files == 0
    # 再打开同一个数据库验证另一平台每层关系完整及检查点不丢失。
    from compass_collector.persistence import Database
    other = "taobao" if platform == "compass" else "compass"
    database = Database(tmp_path / "runtime/data" / f"{other}.db")
    try:
        with database.session_factory() as session:
            assert [batch.platform for batch in session.scalars(select(CollectionBatch))] == [other]
            assert len(session.scalars(select(CategoryRun)).all()) == 2
            assert len(session.scalars(select(ProductRankEntryModel)).all()) == 1
            assert len(session.scalars(select(ProductRankEntryShopModel)).all()) == 1
            assert len(session.scalars(select(RawResponse)).all()) == 1
        assert database.scheduler_checkpoint(f"{platform}_task") is None
        assert database.scheduler_checkpoint(f"{other}_task") is not None
    finally:
        database.close()
    for name in ("exports", "raw", "artifacts", "web-publication"):
        assert not (tmp_path / "runtime" / name / platform).exists()
        assert (tmp_path / "runtime" / name / other).exists()
    assert not (tmp_path / "runtime/exports/2026-10-02" / f"{platform}_task").exists()
    assert (tmp_path / "runtime/exports/2026-10-02" / f"{other}_task" / "old.csv").is_file()
    assert all(file.read_text() == "preserve" for file in preserved)


@pytest.mark.parametrize("lock_name,role", [("scheduler.lock", "scheduler"), ("collection.lock", "collection")])
def test_platform_cleanup_respects_running_collection_and_scheduler(tmp_path, lock_name, role):
    """An occupied runtime must reject cleanup before touching its data."""
    # 即使Profile来自外部工作树，采集协调仍使用同一runtime锁。
    databases = create_both_platforms(tmp_path)
    for database in databases.values():
        database.close()
    with ProcessLock(PlatformRuntime(tmp_path / "runtime", "taobao").lock_path(role), role):
        with pytest.raises(RuntimeLockBusy):
            clear_local_data_with_locks(tmp_path / "runtime", tmp_path / "runtime/data/taobao.db", platform="taobao")
    assert (tmp_path / "runtime/raw/taobao").is_dir()


def test_platform_cleanup_rejects_symlinked_parent_before_database_mutation(tmp_path):
    """A malicious runtime directory alias must not expose another checkout to deletion."""
    # 不触碰真实账号目录，以临时外部目录复现父级符号链接。
    databases = create_both_platforms(tmp_path)
    for database in databases.values():
        database.close()
    # 创建新的web-publication父目录别名，其余真实数据不移动。
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "alias-runtime"
    root.mkdir()
    (root / "web-publication").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="outside runtime"):
        clear_local_data_with_locks(root, root / "data/collector.db", platform="taobao")
    assert (tmp_path / "runtime/raw/taobao").is_dir() and external.is_dir()
