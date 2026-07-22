"""Stage-three SQLite state-machine and raw-page indexing tests."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    DiscoveredCategory,
    RawPageRecord,
)
from compass_collector.persistence import (
    CategoryRun,
    CollectionBatch,
    Database,
    RawResponse,
    upgrade_database,
)


# 固定批次时间让 SQLite 快照断言保持稳定。
PLANNED_AT = datetime(2026, 7, 17, 14, 0)
# 分类开始时间与批次时间分开，便于验证状态迁移。
STARTED_AT = datetime(2026, 7, 17, 14, 0, 1)
# 分类结束时间用于所有终态断言。
FINISHED_AT = datetime(2026, 7, 17, 14, 0, 9)


def prepare_database(
    tmp_path: Path,
    *,
    category_count: int = 4,
) -> tuple[Database, tuple[CategoryRunPlan, ...]]:
    """Create one running batch with deterministic pending category rows."""

    # 每个测试使用独立迁移数据库，避免共享事务状态。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    # Database 是本测试唯一的持久化入口。
    database = Database(database_path)
    # Manifest 路径只作为数据库中的安全索引。
    manifest_path = tmp_path / "runtime" / "raw" / "manifest.json"
    database.create_batch(
        batch_id="batch-stage-three",
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=PLANNED_AT,
        mode="normal",
        brand_type=0,
        price_bin="10001-?",
        manifest_path=manifest_path,
        started_at=PLANNED_AT,
    )
    # 分类树 raw 必须真实存在后才能创建分类运行。
    category_tree_path = manifest_path.parent / "category-tree.json.gz"
    category_tree_path.parent.mkdir(parents=True, exist_ok=True)
    category_tree_path.write_bytes(b"category-tree")
    database.record_category_tree_raw(
        batch_id="batch-stage-three",
        category_tree_raw_path=category_tree_path,
    )
    # 动态分类数量用于覆盖顺序、多个失败分类和剩余分类收口。
    categories = tuple(
        DiscoveredCategory(
            discovery_order=category_index,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="200",
            level2_category_name="休闲食品",
            category_id=f"category-id-{category_index}",
            category_name=f"三级分类{category_index}",
        )
        for category_index in range(1, category_count + 1)
    )
    # 当次发现结果固定根分类并保留接口顺序。
    discovery = CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=categories,
    )
    # category_run_id 由编排层预先分配并跨文件、数据库复用。
    category_run_plans = tuple(
        CategoryRunPlan(
            category_run_id=f"category-run-{category.discovery_order}",
            category=category,
        )
        for category in categories
    )
    database.create_category_runs(
        batch_id="batch-stage-three",
        discovery=discovery,
        category_run_plans=category_run_plans,
    )
    return database, category_run_plans


def create_raw_page(
    tmp_path: Path,
    *,
    category_run_id: str,
    page_no: int,
    item_count: int,
) -> RawPageRecord:
    """Create one existing raw placeholder accepted by the database interface."""

    # 文件名同时包含分类和页码，避免测试页互相覆盖。
    raw_page_path = (
        tmp_path / "raw-pages" / f"{category_run_id}-page-{page_no:03d}.json.gz"
    )
    raw_page_path.parent.mkdir(parents=True, exist_ok=True)
    raw_page_path.write_bytes(b"raw-page")
    return RawPageRecord(
        page_no=page_no,
        path=raw_page_path,
        item_count=item_count,
        captured_at=STARTED_AT + timedelta(seconds=page_no),
    )


def test_category_runs_can_start_concurrently_after_discovery(
    tmp_path: Path,
) -> None:
    """Allow parallel category lifecycles after the full discovery plan is registered."""

    # 四个 pending 分类用于验证一级分类并发下可独立进入 running。
    database, category_run_plans = prepare_database(tmp_path)
    try:
        database.start_category_run(
            category_run_plans[0].category_run_id,
            STARTED_AT,
        )
        snapshot = database.start_category_run(
            category_run_plans[1].category_run_id,
            STARTED_AT,
        )
    finally:
        database.close()

    assert snapshot.status == "running"
    assert [category.status for category in snapshot.categories] == [
        "running",
        "running",
        "pending",
        "pending",
    ]


def test_records_eight_pages_and_finishes_seventy_three_items(
    tmp_path: Path,
) -> None:
    """Persist all pages derived from total instead of applying an item cap."""

    # 单分类足以验证 73 条需要完整保存 8 页。
    database, category_run_plans = prepare_database(tmp_path, category_count=1)
    try:
        # 当前分类必须先进入 running。
        category_run_id = category_run_plans[0].category_run_id
        database.start_category_run(category_run_id, STARTED_AT)
        # 前七页各十条，末页三条，累计严格等于 total。
        for page_no in range(1, 9):
            item_count = 10 if page_no < 8 else 3
            # raw 文件先存在，数据库随后登记安全索引。
            raw_page = create_raw_page(
                tmp_path,
                category_run_id=category_run_id,
                page_no=page_no,
                item_count=item_count,
            )
            snapshot = database.record_category_page(
                category_run_id,
                raw_page,
                73,
                8,
            )
        # 成功接口只验证首页计划和已保存进度，不重新覆盖 total。
        snapshot = database.finish_category_success(
            category_run_id,
            73,
            8,
            FINISHED_AT,
        )
        with database.session_factory() as session:
            # raw_responses 必须有八个连续索引。
            raw_page_count = session.scalar(
                select(func.count()).select_from(RawResponse)
            )
    finally:
        database.close()

    assert raw_page_count == 8
    assert snapshot.successful_category_count == 1
    assert snapshot.saved_page_count == 8
    assert snapshot.collected_item_count == 73
    assert snapshot.categories[0].status == "success"
    assert snapshot.categories[0].saved_item_count == 73


def test_zero_total_still_records_page_one_and_succeeds(tmp_path: Path) -> None:
    """Treat one empty first page as a valid zero-item category result."""

    # 单分类运行用于验证 total=0 的合法计划仍为一页。
    database, category_run_plans = prepare_database(tmp_path, category_count=1)
    try:
        # 分类从 pending 进入 running 后保存空第一页。
        category_run_id = category_run_plans[0].category_run_id
        database.start_category_run(category_run_id, STARTED_AT)
        # raw 文件仍需真实存在，即使该页没有商品。
        raw_page = create_raw_page(
            tmp_path,
            category_run_id=category_run_id,
            page_no=1,
            item_count=0,
        )
        database.record_category_page(category_run_id, raw_page, 0, 1)
        # 一页零条与首页 total 完全一致，应允许成功。
        snapshot = database.finish_category_success(
            category_run_id,
            0,
            1,
            FINISHED_AT,
        )
    finally:
        database.close()

    assert snapshot.saved_page_count == 1
    assert snapshot.collected_item_count == 0
    assert snapshot.categories[0].status == "success"


def test_rejects_missing_raw_jump_page_and_changed_total(tmp_path: Path) -> None:
    """Keep raw-first, continuous pagination, and immutable page-one totals."""

    # 单分类运行隔离所有分页拒绝路径。
    database, category_run_plans = prepare_database(tmp_path, category_count=1)
    try:
        category_run_id = category_run_plans[0].category_run_id
        database.start_category_run(category_run_id, STARTED_AT)
        # 不存在的 raw 路径不能进入 raw_responses。
        missing_raw_page = RawPageRecord(
            page_no=1,
            path=tmp_path / "missing.json.gz",
            item_count=10,
            captured_at=STARTED_AT,
        )
        with pytest.raises(FileNotFoundError):
            database.record_category_page(category_run_id, missing_raw_page, 20, 2)
        # 合法第一页初始化 total 和目标页数。
        first_raw_page = create_raw_page(
            tmp_path,
            category_run_id=category_run_id,
            page_no=1,
            item_count=10,
        )
        database.record_category_page(category_run_id, first_raw_page, 20, 2)
        # 跳过第二页直接登记第三页必须失败。
        third_raw_page = create_raw_page(
            tmp_path,
            category_run_id=category_run_id,
            page_no=3,
            item_count=0,
        )
        with pytest.raises(RuntimeError, match="continuously"):
            database.record_category_page(category_run_id, third_raw_page, 20, 2)
        # 第二页携带变化后的 total 同样不得写入。
        second_raw_page = create_raw_page(
            tmp_path,
            category_run_id=category_run_id,
            page_no=2,
            item_count=10,
        )
        with pytest.raises(RuntimeError, match="changed"):
            database.record_category_page(category_run_id, second_raw_page, 21, 3)
        # 失败尝试后权威快照仍只有第一页。
        snapshot = database.collection_snapshot("batch-stage-three")
    finally:
        database.close()

    assert snapshot.saved_page_count == 1
    assert snapshot.collected_item_count == 10
    assert snapshot.categories[0].api_total == 20
    assert snapshot.categories[0].target_page_count == 2


@pytest.mark.parametrize("error_category", ("duplicate_product", "duplicate_rank"))
def test_complete_ranking_failure_can_reference_the_last_saved_page(
    tmp_path: Path,
    error_category: str,
) -> None:
    """Fail a category after all raw pages are saved but final ranking validation fails."""

    # 单页分类模拟 validate_complete_ranking 在 raw 与数据库登记之后执行。
    database, category_run_plans = prepare_database(tmp_path, category_count=1)
    try:
        category_run_id = category_run_plans[0].category_run_id
        database.start_category_run(category_run_id, STARTED_AT)
        # 唯一页面先完成 raw-first 登记。
        raw_page = create_raw_page(
            tmp_path,
            category_run_id=category_run_id,
            page_no=1,
            item_count=10,
        )
        database.record_category_page(category_run_id, raw_page, 10, 1)
        # 完整榜单重复校验失败应允许 failed_page 指向已保存的最后一页。
        snapshot = database.finish_category_failure(
            category_run_id,
            1,
            error_category,
            FINISHED_AT,
        )
    finally:
        database.close()

    assert snapshot.status == "running"
    assert snapshot.failed_category_count == 1
    assert snapshot.saved_page_count == 1
    assert snapshot.collected_item_count == 10
    assert snapshot.categories[0].status == "failed"
    assert snapshot.categories[0].failed_page == 1
    assert snapshot.categories[0].error_category == error_category


def test_multiple_ordinary_failures_remain_publishable_category_outcomes(
    tmp_path: Path,
) -> None:
    """Allow three category failures without forcing an early batch termination."""

    # 四分类中的前三个失败均可独立收口，最后一个仍保留 pending。
    database, category_run_plans = prepare_database(tmp_path)
    try:
        for category_run_plan in category_run_plans[:3]:
            # 任意数量普通失败都只结束当前分类并允许继续。
            database.start_category_run(
                category_run_plan.category_run_id,
                STARTED_AT,
            )
            snapshot = database.finish_category_failure(
                category_run_plan.category_run_id,
                1,
                "request_failed",
                FINISHED_AT,
            )
            assert snapshot.status == "running"
        with database.session_factory() as session:
            # SQLite 读取证明三个失败不会影响尚未开始的第四个分类。
            pending_category_count = session.scalar(
                select(func.count())
                .select_from(CategoryRun)
                .where(CategoryRun.status == "pending")
            )
            batch = session.get(CollectionBatch, "batch-stage-three")
    finally:
        database.close()

    assert batch is not None
    assert batch.status == "running"
    assert pending_category_count == 1
    assert snapshot.failed_category_count == 3
    assert snapshot.not_started_category_count == 0
    assert [category.status for category in snapshot.categories] == [
        "failed",
        "failed",
        "failed",
        "pending",
    ]


@pytest.mark.parametrize(
    ("batch_status", "current_status", "error_category"),
    (
        ("auth_required", "failed", "auth_required"),
        ("interrupted", "interrupted", "interrupted"),
        ("abandoned", "abandoned", "internal_error"),
    ),
)
def test_terminal_batch_keeps_saved_progress_and_closes_pending_categories(
    tmp_path: Path,
    batch_status: str,
    current_status: str,
    error_category: str,
) -> None:
    """Preserve saved raw counts for auth or manual interruption termination."""

    # 三分类覆盖当前分类和两个尚未开始分类。
    database, category_run_plans = prepare_database(tmp_path, category_count=3)
    try:
        category_run_id = category_run_plans[0].category_run_id
        database.start_category_run(category_run_id, STARTED_AT)
        # 当前分类先成功登记一页，再在第二页终止。
        raw_page = create_raw_page(
            tmp_path,
            category_run_id=category_run_id,
            page_no=1,
            item_count=10,
        )
        database.record_category_page(category_run_id, raw_page, 20, 2)
        # error_category 对应批次终止原因，不保存异常原文。
        snapshot = database.terminate_collection_batch(
            "batch-stage-three",
            batch_status,
            error_category,
            FINISHED_AT,
            current_category_run_id=category_run_id,
            failed_page=2,
        )
    finally:
        database.close()

    assert snapshot.status == batch_status
    assert snapshot.saved_page_count == 1
    assert snapshot.collected_item_count == 10
    assert [category.status for category in snapshot.categories] == [
        current_status,
        "not_started",
        "not_started",
    ]


def test_terminal_batch_between_categories_needs_no_current_run(
    tmp_path: Path,
) -> None:
    """Terminate between categories and mark every pending category not started."""

    # 尚未启动任何分类时不存在 current_category_run_id。
    database, _ = prepare_database(tmp_path, category_count=2)
    try:
        snapshot = database.terminate_collection_batch(
            "batch-stage-three",
            "interrupted",
            "interrupted",
            FINISHED_AT,
        )
    finally:
        database.close()

    assert snapshot.status == "interrupted"
    assert snapshot.not_started_category_count == 2
    assert [category.status for category in snapshot.categories] == [
        "not_started",
        "not_started",
    ]


def test_terminal_batch_without_current_category_closes_parallel_running_categories(
    tmp_path: Path,
) -> None:
    """Close every active category when a worker fails before naming one category."""

    # 两个运行中分类模拟一级分类工作线程在回报结果前异常。
    database, category_run_plans = prepare_database(tmp_path, category_count=3)
    try:
        for category_run_plan in category_run_plans[:2]:
            database.start_category_run(category_run_plan.category_run_id, STARTED_AT)
        snapshot = database.terminate_collection_batch(
            "batch-stage-three",
            "abandoned",
            "internal_error",
            FINISHED_AT,
        )
    finally:
        database.close()

    assert snapshot.status == "abandoned"
    assert [category.status for category in snapshot.categories] == [
        "abandoned",
        "abandoned",
        "not_started",
    ]
