"""Clean v1 schema and batch-publication semantics tests."""

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from compass_collector.persistence import (
    CategoryRun,
    CollectionBatch,
    Database,
    ProductRankEntryModel,
    RawResponse,
    SchedulerCheckpoint,
    upgrade_database,
)


# 所有时间固定为北京墙上时间，便于 SQLite 断言。
PLANNED_AT = datetime(2026, 7, 17, 14, 0)
STARTED_AT = datetime(2026, 7, 17, 14, 0, 1)
FINISHED_AT = datetime(2026, 7, 17, 14, 8)


def create_database(tmp_path: Path) -> Database:
    """Create one fully migrated test database below the pytest temp root."""

    # 每个测试使用独立 SQLite，不触碰工程 runtime。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    return Database(database_path)


def build_running_batch(*, batch_id: str, discovered_categories: int) -> CollectionBatch:
    """Build one valid running batch for relationship and uniqueness tests."""

    return CollectionBatch(
        id=batch_id,
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=PLANNED_AT,
        mode="normal",
        status="running",
        version=None,
        root_category_id="13",
        root_category_name="食品饮料",
        manifest_path=f"runtime/raw/2026-07-17/{batch_id}/manifest.json",
        category_tree_raw_path=f"runtime/raw/2026-07-17/{batch_id}/category-tree.json.gz",
        csv_path=None,
        discovered_category_count=discovered_categories,
        successful_category_count=0,
        failed_category_count=0,
        not_started_category_count=0,
        saved_page_count=0,
        collected_item_count=0,
        error_category=None,
        started_at=STARTED_AT,
        finished_at=None,
        published_at=None,
    )


def build_pending_category(
    *,
    category_run_id: str,
    batch_id: str,
    discovery_order: int,
    category_id: str,
    category_name: str,
) -> CategoryRun:
    """Build one pending level-three category snapshot."""

    return CategoryRun(
        id=category_run_id,
        batch_id=batch_id,
        discovery_order=discovery_order,
        level1_category_id="13",
        level1_category_name="食品饮料",
        level2_category_id="1000001823",
        level2_category_name="水饮冲调",
        category_id=category_id,
        category_name=category_name,
        status="pending",
        api_total=None,
        target_page_count=None,
        saved_page_count=0,
        saved_item_count=0,
        failed_page=None,
        error_category=None,
        started_at=None,
        finished_at=None,
    )


def build_rank_entry(*, category_run_id: str) -> ProductRankEntryModel:
    """Build one valid rank-one product that may repeat across categories."""

    return ProductRankEntryModel(
        category_run_id=category_run_id,
        captured_at=STARTED_AT,
        page_no=1,
        rank=1,
        product_id="same-product",
        product_name="跨分类示例商品",
        newly_on_ranking=False,
        pay_amount_min_value=100,
        pay_amount_max_value=200,
        pay_amount_unit="price",
        pay_combo_count_min_value=10,
        pay_combo_count_max_value=20,
        pay_combo_count_unit="count",
    )


def test_clean_migration_creates_only_the_new_baseline_tables(tmp_path: Path) -> None:
    """Create the clean v1 schema without the removed collection_runs table."""

    # 临时数据库执行仓库当前唯一迁移。
    database = create_database(tmp_path)
    try:
        # Inspector 用于核对真实 SQLite 表和关键字段。
        database_inspector = inspect(database.engine)
        table_names = set(database_inspector.get_table_names())
        batch_columns = {
            column["name"]
            for column in database_inspector.get_columns("collection_batches")
        }
        category_columns = {
            column["name"] for column in database_inspector.get_columns("category_runs")
        }
        raw_columns = {
            column["name"] for column in database_inspector.get_columns("raw_responses")
        }
        product_columns = {
            column["name"]
            for column in database_inspector.get_columns("product_rank_entries")
        }
    finally:
        database.close()

    assert table_names == {
        "alembic_version",
        "collection_batches",
        "category_runs",
        "raw_responses",
        "product_rank_entries",
        "product_rank_entry_shops",
        "scheduler_checkpoints",
    }
    assert "collection_runs" not in table_names
    assert {
        "mode",
        "published_at",
        "category_tree_raw_path",
        "brand_type",
        "price_bin",
    } <= batch_columns
    assert {"discovery_order", "category_id", "target_page_count"} <= category_columns
    assert "category_run_id" in raw_columns
    assert "run_id" not in raw_columns
    assert "image_url" in product_columns


def test_rank_and_product_are_unique_inside_each_category_only(tmp_path: Path) -> None:
    """Allow one product and rank to repeat across categories but not within one."""

    # 两个分类共用一个顶层批次。
    database = create_database(tmp_path)
    try:
        with database.session_factory.begin() as session:
            session.add(build_running_batch(batch_id="batch-one", discovered_categories=2))
            # 先落盘父批次，再创建依赖它的分类运行。
            session.flush()
            session.add_all(
                [
                    build_pending_category(
                        category_run_id="category-one",
                        batch_id="batch-one",
                        discovery_order=1,
                        category_id="leaf-one",
                        category_name="茶叶",
                    ),
                    build_pending_category(
                        category_run_id="category-two",
                        batch_id="batch-one",
                        discovery_order=2,
                        category_id="leaf-two",
                        category_name="冲饮谷物",
                    ),
                ]
            )
        with database.session_factory.begin() as session:
            # 相同商品和第一名可合法出现在不同三级分类。
            session.add_all(
                [
                    build_rank_entry(category_run_id="category-one"),
                    build_rank_entry(category_run_id="category-two"),
                ]
            )
        with database.session_factory() as session:
            # 两个合法跨分类记录用于确认新唯一边界。
            entry_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
        assert entry_count == 2

        with pytest.raises(IntegrityError):
            with database.session_factory.begin() as session:
                # 同一分类内重复 rank/product 必须由 SQLite 拒绝。
                session.add(build_rank_entry(category_run_id="category-one"))
    finally:
        database.close()


def test_published_at_is_the_only_official_publication_boundary(tmp_path: Path) -> None:
    """Treat partial success as published while keeping dry-run unofficial."""

    # dry-run 和正式批次可共用一个计划时间。
    database = create_database(tmp_path)
    try:
        with database.session_factory.begin() as session:
            session.add_all(
                [
                    CollectionBatch(
                        id="dry-run-batch",
                        task_id="product_hot_sale_food_level3",
                        business_date=date(2026, 7, 17),
                        planned_at=PLANNED_AT,
                        mode="dry_run",
                        status="success",
                        version=None,
                        root_category_id="13",
                        root_category_name="食品饮料",
                        manifest_path="runtime/raw/dry-run/manifest.json",
                        category_tree_raw_path="runtime/raw/dry-run/category-tree.json.gz",
                        csv_path=None,
                        discovered_category_count=1,
                        successful_category_count=1,
                        failed_category_count=0,
                        not_started_category_count=0,
                        saved_page_count=1,
                        collected_item_count=10,
                        error_category=None,
                        started_at=STARTED_AT,
                        finished_at=FINISHED_AT,
                        published_at=None,
                    ),
                    CollectionBatch(
                        id="published-batch",
                        task_id="product_hot_sale_food_level3",
                        business_date=date(2026, 7, 17),
                        planned_at=PLANNED_AT,
                        mode="normal",
                        status="partial_success",
                        version=1,
                        root_category_id="13",
                        root_category_name="食品饮料",
                        manifest_path="runtime/raw/published/manifest.json",
                        category_tree_raw_path="runtime/raw/published/category-tree.json.gz",
                        csv_path="runtime/exports/食品饮料三级分类商品实时榜.csv",
                        discovered_category_count=2,
                        successful_category_count=1,
                        failed_category_count=1,
                        not_started_category_count=0,
                        saved_page_count=20,
                        collected_item_count=190,
                        error_category=None,
                        started_at=STARTED_AT,
                        finished_at=FINISHED_AT,
                        published_at=FINISHED_AT,
                    ),
                ]
            )

        # 幂等查询必须命中 partial_success 正式批次。
        published_batch = database.successful_batch(
            "product_hot_sale_food_level3",
            PLANNED_AT,
        )
        assert published_batch is not None
        assert published_batch.batch_id == "published-batch"
        assert published_batch.version == 1
        assert database.next_version("product_hot_sale_food_level3", PLANNED_AT) == 2

        with pytest.raises(IntegrityError):
            with database.session_factory.begin() as session:
                # 正式 success 没有 published_at 必须被数据库约束拒绝。
                session.add(
                    CollectionBatch(
                        id="invalid-official-batch",
                        task_id="invalid-task",
                        business_date=date(2026, 7, 17),
                        planned_at=PLANNED_AT,
                        mode="normal",
                        status="success",
                        version=1,
                        root_category_id="13",
                        root_category_name="食品饮料",
                        manifest_path="runtime/raw/invalid/manifest.json",
                        category_tree_raw_path="runtime/raw/invalid/category-tree.json.gz",
                        csv_path="runtime/exports/invalid.csv",
                        discovered_category_count=0,
                        successful_category_count=0,
                        failed_category_count=0,
                        not_started_category_count=0,
                        saved_page_count=0,
                        collected_item_count=0,
                        error_category=None,
                        started_at=STARTED_AT,
                        finished_at=FINISHED_AT,
                        published_at=None,
                    )
                )
    finally:
        database.close()


@pytest.mark.parametrize(
    (
        "status",
        "discovered_count",
        "successful_count",
        "failed_count",
        "not_started_count",
    ),
    [
        ("success", 0, 0, 0, 0),
        ("success", 2, 1, 1, 0),
        ("partial_success", 2, 0, 2, 0),
        ("partial_success", 3, 1, 1, 1),
    ],
)
def test_publication_status_enforces_the_category_failure_threshold(
    tmp_path: Path,
    status: str,
    discovered_count: int,
    successful_count: int,
    failed_count: int,
    not_started_count: int,
) -> None:
    """Reject success labels that contradict discovered category outcomes."""

    # dry-run 不需要版本或 CSV，因此测试只聚焦分类计数约束。
    database = create_database(tmp_path)
    try:
        with pytest.raises(IntegrityError):
            with database.session_factory.begin() as session:
                # 这些组合分别覆盖空分类、错标 success 和没有成功分类的部分结果。
                session.add(
                    CollectionBatch(
                        id="invalid-category-counts",
                        task_id="product_hot_sale_food_level3",
                        business_date=date(2026, 7, 17),
                        planned_at=PLANNED_AT,
                        mode="dry_run",
                        status=status,
                        version=None,
                        root_category_id="13",
                        root_category_name="食品饮料",
                        manifest_path="runtime/raw/invalid-counts/manifest.json",
                        category_tree_raw_path=(
                            "runtime/raw/invalid-counts/category-tree.json.gz"
                        ),
                        csv_path=None,
                        discovered_category_count=discovered_count,
                        successful_category_count=successful_count,
                        failed_category_count=failed_count,
                        not_started_category_count=not_started_count,
                        saved_page_count=0,
                        collected_item_count=0,
                        error_category=None,
                        started_at=STARTED_AT,
                        finished_at=FINISHED_AT,
                        published_at=None,
                    )
                )
    finally:
        database.close()


def test_scheduler_terminal_batches_and_checkpoint_use_the_new_model(
    tmp_path: Path,
) -> None:
    """Keep Scheduler missed/busy semantics without a collection_runs table."""

    # Scheduler-only 记录不创建分类、raw 或版本。
    database = create_database(tmp_path)
    try:
        missed_batch_id = database.record_missed_run(
            task_id="product_hot_sale_food_level3",
            business_date=date(2026, 7, 16),
            planned_at=datetime(2026, 7, 16, 14, 0),
            error_category="cross_day_missed",
            recorded_at=datetime(2026, 7, 17, 9, 0),
        )
        duplicate_batch_id = database.record_missed_run(
            task_id="product_hot_sale_food_level3",
            business_date=date(2026, 7, 16),
            planned_at=datetime(2026, 7, 16, 14, 0),
            error_category="cross_day_missed",
            recorded_at=datetime(2026, 7, 17, 9, 1),
        )
        # 检查点仍保持原有按任务 upsert 语义。
        database.set_scheduler_checkpoint(
            "product_hot_sale_food_level3",
            datetime(2026, 7, 17, 9, 2),
        )
        rows = database.recent_status(limit=5)
        with database.session_factory() as session:
            # 新模型中 Scheduler-only 批次不会造 raw 行。
            raw_count = session.scalar(select(func.count()).select_from(RawResponse))
            checkpoint_count = session.scalar(
                select(func.count()).select_from(SchedulerCheckpoint)
            )
    finally:
        database.close()

    assert missed_batch_id is not None
    assert duplicate_batch_id is None
    assert len(rows) == 1
    assert rows[0].batch_id == missed_batch_id
    assert rows[0].status == "missed"
    assert rows[0].version is None
    assert rows[0].csv_path is None
    assert raw_count == 0
    assert checkpoint_count == 1
