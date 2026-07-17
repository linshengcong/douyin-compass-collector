"""Safety tests for developer-only local collection data cleanup."""

from datetime import date, datetime
from pathlib import Path

import pytest

from compass_collector.local_data import (
    DISPOSABLE_RUNTIME_DIRECTORIES,
    clear_local_data,
    clear_local_data_with_locks,
)
from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    DiscoveredCategory,
)
from compass_collector.raw_storage import BatchStorage
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy


def test_clear_local_data_removes_only_allowlisted_collection_material(
    tmp_path: Path,
) -> None:
    """Delete collected data while preserving login, locks, backups, and unknown files."""

    # 临时 runtime 完整模拟开发机目录，不触碰真实采集数据。
    runtime_root = tmp_path / "runtime"
    # 数据库主文件和 sidecar 都应被清理。
    database_path = runtime_root / "data" / "collector.db"
    database_path.parent.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{database_path}{suffix}").write_text("database", encoding="utf-8")
    # data 目录中的未知文件不属于白名单。
    unknown_database_file = database_path.parent / "keep.db"
    unknown_database_file.write_text("keep", encoding="utf-8")

    for directory_name in DISPOSABLE_RUNTIME_DIRECTORIES:
        # 每个可丢弃目录都放入一个模拟业务文件。
        disposable_file = runtime_root / directory_name / "nested" / "data.txt"
        disposable_file.parent.mkdir(parents=True)
        disposable_file.write_text("delete", encoding="utf-8")

    # Chrome Profile、锁、备份和 runtime 未知文件必须保留。
    preserved_files = (
        runtime_root / "browser-profile" / "Default" / "Login Data",
        runtime_root / "locks" / "gui.lock",
        runtime_root / "backups" / "collector-backup.db",
        runtime_root / "custom.keep",
    )
    for preserved_file in preserved_files:
        preserved_file.parent.mkdir(parents=True, exist_ok=True)
        preserved_file.write_text("preserve", encoding="utf-8")

    summary = clear_local_data(runtime_root, database_path)

    assert summary.database_files == 4
    assert summary.runtime_directories == len(DISPOSABLE_RUNTIME_DIRECTORIES)
    assert summary.failures == 0
    assert not database_path.exists()
    assert unknown_database_file.read_text(encoding="utf-8") == "keep"
    for directory_name in DISPOSABLE_RUNTIME_DIRECTORIES:
        # 可丢弃目录会被重建为空目录。
        recreated_directory = runtime_root / directory_name
        assert recreated_directory.is_dir()
        assert list(recreated_directory.iterdir()) == []
    for preserved_file in preserved_files:
        assert preserved_file.read_text(encoding="utf-8") == "preserve"


def test_clear_local_data_removes_real_batch_storage_layout(tmp_path: Path) -> None:
    """Remove current raw and artifact layouts created by the production adapter."""

    # 真实 BatchStorage 在临时 runtime 中建立当前日期/任务/批次层级。
    runtime_root = tmp_path / "runtime"
    # 固定时间让 Manifest 和目录断言不依赖当前日期。
    captured_at = datetime(2026, 7, 17, 14, 0)
    storage = BatchStorage(
        runtime_root=runtime_root,
        batch_id="cleanup-batch",
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=captured_at,
        mode="normal",
        started_at=captured_at,
    )
    # 分类树先按生产顺序写入并登记到 Manifest。
    category_tree_path = storage.write_category_tree({"st": 0, "data": {}})
    storage.record_category_tree_saved(category_tree_path, captured_at=captured_at)
    # 单个三级分类足以生成真实 categories/<category_run_id>/page 路径。
    category = DiscoveredCategory(
        discovery_order=1,
        level1_category_id="13",
        level1_category_name="食品饮料",
        level2_category_id="level-two",
        level2_category_name="二级分类",
        category_id="level-three",
        category_name="三级分类",
    )
    # 分类发现对象和运行计划共同登记分页目录白名单。
    discovery = CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=(category,),
    )
    category_plan = CategoryRunPlan(
        category_run_id="cleanup-category-run",
        category=category,
    )
    storage.record_discovered_categories(discovery, (category_plan,))
    storage.write_category_page(
        category_plan.category_run_id,
        1,
        {"st": 0, "data": {"data_result": []}},
    )
    # 批次失败材料生成当前 artifacts 日期/任务/批次布局。
    storage.save_failure(
        status_code=500,
        error_category="http_error",
        response_body=b"safe fixture body",
        failed_step="category_tree_request_or_contract",
        exception_type="HttpResponseError",
        safe_endpoint_path="/compass_api/config_center/category/cate_list",
    )
    # SQLite 主库和 sidecar 复用生产清理白名单。
    database_path = runtime_root / "data" / "collector.db"
    database_path.parent.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{database_path}{suffix}").write_text("database", encoding="utf-8")
    # Chrome Profile 和未知 runtime 文件验证清理边界不随真实布局扩大。
    profile_file = runtime_root / "browser-profile" / "Default" / "Login Data"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_text("preserve", encoding="utf-8")
    unknown_file = runtime_root / "custom.keep"
    unknown_file.write_text("preserve", encoding="utf-8")

    summary = clear_local_data(runtime_root, database_path)

    assert summary.succeeded is True
    assert summary.database_files == 4
    assert not storage.batch_dir.exists()
    assert not storage.artifact_dir.exists()
    assert not database_path.exists()
    assert profile_file.read_text(encoding="utf-8") == "preserve"
    assert unknown_file.read_text(encoding="utf-8") == "preserve"


def test_clear_local_data_rejects_database_outside_runtime_before_deletion(
    tmp_path: Path,
) -> None:
    """Refuse an unsafe configured database path without partially clearing runtime."""

    # runtime 中的 CSV 用于证明安全检查发生在任何删除之前。
    runtime_root = tmp_path / "runtime"
    csv_file = runtime_root / "exports" / "keep.csv"
    csv_file.parent.mkdir(parents=True)
    csv_file.write_text("keep", encoding="utf-8")
    # 配置到 runtime 之外的数据库必须被拒绝。
    unsafe_database = tmp_path / "outside" / "collector.db"
    unsafe_database.parent.mkdir(parents=True)
    unsafe_database.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError):
        clear_local_data(runtime_root, unsafe_database)

    assert csv_file.read_text(encoding="utf-8") == "keep"
    assert unsafe_database.read_text(encoding="utf-8") == "keep"


def test_clear_local_data_rejects_database_inside_browser_profile(
    tmp_path: Path,
) -> None:
    """Protect login state even when the configured database points inside runtime."""

    # Profile 中的模拟文件即使位于 runtime 内也不属于数据库删除边界。
    runtime_root = tmp_path / "runtime"
    protected_file = runtime_root / "browser-profile" / "Default" / "Login Data"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime/data"):
        clear_local_data(runtime_root, protected_file)

    assert protected_file.read_text(encoding="utf-8") == "preserve"


def test_clear_local_data_rejects_database_symlink(
    tmp_path: Path,
) -> None:
    """Refuse a database symlink even when its link name is inside runtime/data."""

    # 受保护文件模拟数据库符号链接的外部目标。
    runtime_root = tmp_path / "runtime"
    protected_file = runtime_root / "browser-profile" / "Default" / "Login Data"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("preserve", encoding="utf-8")
    # 数据库链接名在合法目录中，但仍必须被拒绝。
    database_path = runtime_root / "data" / "collector.db"
    database_path.parent.mkdir(parents=True)
    database_path.symlink_to(protected_file)

    with pytest.raises(ValueError, match="symlink"):
        clear_local_data(runtime_root, database_path)

    assert protected_file.read_text(encoding="utf-8") == "preserve"
    assert database_path.is_symlink()


def test_clear_local_data_rejects_symlinked_runtime_root(tmp_path: Path) -> None:
    """Refuse cleanup when the runtime root itself redirects elsewhere."""

    # 外部 runtime 模拟不应被当前工程删除的数据。
    external_runtime = tmp_path / "external-runtime"
    database_path = external_runtime / "data" / "collector.db"
    database_path.parent.mkdir(parents=True)
    database_path.write_text("preserve", encoding="utf-8")
    # 工程 runtime 只是指向外部目录的链接。
    runtime_link = tmp_path / "runtime"
    runtime_link.symlink_to(external_runtime, target_is_directory=True)

    with pytest.raises(ValueError, match="runtime root"):
        clear_local_data(runtime_link, runtime_link / "data" / "collector.db")

    assert database_path.read_text(encoding="utf-8") == "preserve"


def test_clear_local_data_preserves_external_target_of_runtime_symlink(
    tmp_path: Path,
) -> None:
    """Unlink an allowlisted root symlink without following it outside runtime."""

    # 外部目录模拟不应被删除的其他工程数据。
    external_directory = tmp_path / "external-exports"
    external_directory.mkdir()
    external_file = external_directory / "keep.csv"
    external_file.write_text("keep", encoding="utf-8")
    # exports 根本身是符号链接，清理后应变成本地空目录。
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "exports").symlink_to(external_directory, target_is_directory=True)
    database_path = runtime_root / "data" / "collector.db"

    summary = clear_local_data(runtime_root, database_path)

    assert summary.succeeded is True
    assert external_file.read_text(encoding="utf-8") == "keep"
    assert (runtime_root / "exports").is_dir()
    assert not (runtime_root / "exports").is_symlink()


@pytest.mark.parametrize(
    ("lock_name", "role"),
    [
        ("scheduler.lock", "scheduler"),
        ("collection.lock", "collection"),
    ],
)
def test_clear_local_data_with_locks_refuses_active_runtime_owner(
    tmp_path: Path,
    lock_name: str,
    role: str,
) -> None:
    """Keep data unchanged when Scheduler or collection already owns its lock."""

    # 待保护数据库位于临时 runtime。
    runtime_root = tmp_path / "runtime"
    database_path = runtime_root / "data" / "collector.db"
    database_path.parent.mkdir(parents=True)
    database_path.write_text("keep", encoding="utf-8")
    # 参数化实时锁模拟 Scheduler、login 或采集正在运行。
    active_lock = ProcessLock(
        runtime_root / "locks" / lock_name,
        role,
    )

    with active_lock:
        with pytest.raises(RuntimeLockBusy):
            clear_local_data_with_locks(runtime_root, database_path)

    assert database_path.read_text(encoding="utf-8") == "keep"
