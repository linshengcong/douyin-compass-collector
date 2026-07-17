"""Stage-two batch creation and atomic category-run persistence tests."""

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    DiscoveredCategory,
)
from compass_collector.persistence import (
    CategoryRun,
    CollectionBatch,
    Database,
    upgrade_database,
)


# 固定时间让 SQLite 墙上时间断言稳定。
PLANNED_AT = datetime(2026, 7, 17, 14, 0)
# 批次开始时间与计划时间分开，便于验证字段映射。
STARTED_AT = datetime(2026, 7, 17, 13, 59, 30)
# 分类发现失败时间用于验证批次和分类同时终止。
FINISHED_AT = datetime(2026, 7, 17, 14, 0, 5)


def create_database(tmp_path: Path) -> Database:
    """Create one migrated SQLite database below the pytest temp root."""

    # 每个测试使用独立数据库，避免影响工程 runtime。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    return Database(database_path)


def build_discovery() -> CategoryDiscoveryResult:
    """Build two ordered level-three categories for persistence tests."""

    # 两个分类位于不同二级路径，用于验证完整分类快照。
    categories = (
        DiscoveredCategory(
            discovery_order=1,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="200",
            level2_category_name="休闲食品",
            category_id="301",
            category_name="海味零食",
        ),
        DiscoveredCategory(
            discovery_order=2,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="201",
            level2_category_name="水饮冲调",
            category_id="302",
            category_name="茶叶",
        ),
    )
    return CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=categories,
    )


def build_plans(discovery: CategoryDiscoveryResult) -> tuple[CategoryRunPlan, ...]:
    """Assign deterministic IDs to every discovered category."""

    # 固定 ID 让数据库顺序和外键断言可读。
    return tuple(
        CategoryRunPlan(
            category_run_id=f"category-{category.discovery_order}",
            category=category,
        )
        for category in discovery.categories
    )


def create_running_batch(database: Database, tmp_path: Path) -> Path:
    """Create the common running batch and return its Manifest path."""

    # Manifest 文件本身由 BatchStorage 管理，数据库只保存安全路径。
    manifest_path = tmp_path / "runtime" / "raw" / "manifest.json"
    database.create_batch(
        batch_id="batch-stage-two",
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=PLANNED_AT,
        mode="normal",
        brand_type=0,
        price_bin="10001-?",
        manifest_path=manifest_path,
        started_at=STARTED_AT,
    )
    return manifest_path


def record_category_tree(database: Database, manifest_path: Path) -> Path:
    """Create one local raw placeholder and attach it to the running batch."""

    # Persistence 只接受已经正式存在的分类树文件。
    category_tree_path = manifest_path.parent / "category-tree.json.gz"
    category_tree_path.parent.mkdir(parents=True, exist_ok=True)
    category_tree_path.write_bytes(b"gzip-fixture")
    database.record_category_tree_raw(
        batch_id="batch-stage-two",
        category_tree_raw_path=category_tree_path,
    )
    return category_tree_path


def test_create_batch_and_all_pending_category_runs_atomically(tmp_path: Path) -> None:
    """Persist the raw tree reference and every discovered category in source order."""

    # 阶段二数据库先建立 running 批次。
    database = create_database(tmp_path)
    try:
        # 批次 Manifest 路径用于核对创建参数未被改写。
        manifest_path = create_running_batch(database, tmp_path)
        # 分类树 raw 在解析之前已经原子落盘。
        category_tree_path = record_category_tree(database, manifest_path)
        # 解析结果和预生成 ID 通过一次事务登记。
        discovery = build_discovery()
        category_run_plans = build_plans(discovery)
        database.create_category_runs(
            batch_id="batch-stage-two",
            discovery=discovery,
            category_run_plans=category_run_plans,
        )

        with database.session_factory() as session:
            # 批次摘要必须与当次分类树解析结果一致。
            batch = session.get(CollectionBatch, "batch-stage-two")
            # 分类运行按发现顺序回读，不能依赖主键排序。
            category_runs = session.scalars(
                select(CategoryRun)
                .where(CategoryRun.batch_id == "batch-stage-two")
                .order_by(CategoryRun.discovery_order)
            ).all()

        assert batch is not None
        assert batch.status == "running"
        assert batch.brand_type == 0
        assert batch.price_bin == "10001-?"
        assert batch.manifest_path == str(manifest_path)
        assert batch.category_tree_raw_path == str(category_tree_path)
        assert batch.root_category_id == "13"
        assert batch.root_category_name == "食品饮料"
        assert batch.discovered_category_count == 2
        assert [category_run.id for category_run in category_runs] == [
            "category-1",
            "category-2",
        ]
        assert [category_run.status for category_run in category_runs] == [
            "pending",
            "pending",
        ]
        assert [category_run.category_name for category_run in category_runs] == [
            "海味零食",
            "茶叶",
        ]
    finally:
        database.close()


def test_category_run_insert_failure_rolls_back_batch_discovery_fields(
    tmp_path: Path,
) -> None:
    """Rollback both category rows and batch counters when one insert is invalid."""

    # 唯一约束失败用于模拟批量登记中途异常。
    database = create_database(tmp_path)
    try:
        manifest_path = create_running_batch(database, tmp_path)
        record_category_tree(database, manifest_path)
        # 两个不同计划故意复用同一个 category_id。
        discovery = build_discovery()
        duplicate_category = DiscoveredCategory(
            discovery_order=2,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="201",
            level2_category_name="水饮冲调",
            category_id="301",
            category_name="重复分类",
        )
        # 事务输入本身保持计划与 discovery 一致，让 SQLite 唯一约束负责回滚。
        invalid_discovery = CategoryDiscoveryResult(
            root_category_id=discovery.root_category_id,
            root_category_name=discovery.root_category_name,
            categories=(discovery.categories[0], duplicate_category),
        )
        invalid_plans = build_plans(invalid_discovery)

        with pytest.raises(IntegrityError):
            database.create_category_runs(
                batch_id="batch-stage-two",
                discovery=invalid_discovery,
                category_run_plans=invalid_plans,
            )

        with database.session_factory() as session:
            # 回滚后批次仍处于发现前状态。
            batch = session.get(CollectionBatch, "batch-stage-two")
            # 分类表不能留下第一条半成品。
            category_count = session.scalar(
                select(func.count()).select_from(CategoryRun)
            )

        assert batch is not None
        assert batch.root_category_id is None
        assert batch.root_category_name is None
        assert batch.discovered_category_count == 0
        assert category_count == 0
    finally:
        database.close()


def test_discovery_failure_marks_existing_pending_categories_not_started(
    tmp_path: Path,
) -> None:
    """Close a running batch safely if failure occurs after category registration."""

    # 先模拟分类发现登记成功、后续 Manifest 更新失败的极端路径。
    database = create_database(tmp_path)
    try:
        manifest_path = create_running_batch(database, tmp_path)
        record_category_tree(database, manifest_path)
        # pending 分类用于验证失败收口不会留下悬空运行态。
        discovery = build_discovery()
        database.create_category_runs(
            batch_id="batch-stage-two",
            discovery=discovery,
            category_run_plans=build_plans(discovery),
        )
        database.finish_discovery_failure(
            batch_id="batch-stage-two",
            status="failed",
            error_category="internal_error",
            finished_at=FINISHED_AT,
            root_category_id=discovery.root_category_id,
            root_category_name=discovery.root_category_name,
        )

        with database.session_factory() as session:
            # 批次与分类必须在同一事务内进入终态。
            batch = session.get(CollectionBatch, "batch-stage-two")
            # 分类顺序用于确认全部 pending 都已处理。
            category_runs = session.scalars(
                select(CategoryRun)
                .where(CategoryRun.batch_id == "batch-stage-two")
                .order_by(CategoryRun.discovery_order)
            ).all()

        assert batch is not None
        assert batch.status == "failed"
        assert batch.error_category == "internal_error"
        assert batch.not_started_category_count == 2
        assert batch.finished_at == FINISHED_AT
        assert [category_run.status for category_run in category_runs] == [
            "not_started",
            "not_started",
        ]
        assert [category_run.finished_at for category_run in category_runs] == [
            FINISHED_AT,
            FINISHED_AT,
        ]
    finally:
        database.close()


def test_category_runs_require_an_existing_recorded_category_tree(
    tmp_path: Path,
) -> None:
    """Reject category rows when the batch has no saved raw tree boundary."""

    # 只创建 running 批次，不创建或登记分类树文件。
    database = create_database(tmp_path)
    try:
        create_running_batch(database, tmp_path)
        # 合法解析结果仍不能绕过 raw-first 持久化顺序。
        discovery = build_discovery()
        with pytest.raises(RuntimeError, match="category tree"):
            database.create_category_runs(
                batch_id="batch-stage-two",
                discovery=discovery,
                category_run_plans=build_plans(discovery),
            )
        with database.session_factory() as session:
            # 拒绝后不能留下批次计数或分类行。
            batch = session.get(CollectionBatch, "batch-stage-two")
            category_count = session.scalar(
                select(func.count()).select_from(CategoryRun)
            )

        assert batch is not None
        assert batch.discovered_category_count == 0
        assert category_count == 0
    finally:
        database.close()
