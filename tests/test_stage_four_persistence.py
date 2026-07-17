"""Stage-four dry-run and official publication transaction tests."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compass_collector.errors import PublicationError
from compass_collector.exporter import StagedCsvExport
from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    CollectedCategoryBatch,
    CollectedCategoryRun,
    DiscoveredCategory,
    MetricRange,
    ProductRankEntry,
    ProductShop,
    RawPageRecord,
)
from compass_collector.persistence import (
    CollectionBatch,
    Database,
    ProductRankEntryModel,
    ProductRankEntryShopModel,
    upgrade_database,
)
from compass_collector.raw_storage import BatchStorage


# 批次和分类时间均使用 SQLite 可直接比较的北京时间墙上时间。
BATCH_STARTED_AT = datetime(2026, 7, 17, 14, 0)
# 分类开始时间在批次之后，避免生命周期字段混淆。
CATEGORY_STARTED_AT = datetime(2026, 7, 17, 14, 0, 1)
# 分类完成时间用于构造 success 与 failed 终态。
CATEGORY_FINISHED_AT = datetime(2026, 7, 17, 14, 0, 5)
# 阶段三返回的批次完成时间用于正式发布 finished_at。
COLLECTION_FINISHED_AT = datetime(2026, 7, 17, 14, 0, 8)
# 正式发布时间非空是官方发布唯一判据。
PUBLISHED_AT = datetime(2026, 7, 17, 14, 0, 9)


def build_entry(*, captured_at: datetime) -> ProductRankEntry:
    """Build one valid product that can repeat across different categories."""

    # 所有分类故意复用相同 product_id 与 rank，验证唯一约束按分类隔离。
    return ProductRankEntry(
        page_no=1,
        captured_at=captured_at,
        rank=1,
        product_id="shared-product",
        product_name="跨分类共享商品",
        newly_on_ranking=False,
        pay_amount=MetricRange(
            min_value=100,
            max_value=200,
            unit="price",
        ),
        pay_combo_count=MetricRange(
            min_value=10,
            max_value=20,
            unit="number",
        ),
        shops=(
            ProductShop(
                position=0,
                shop_id="shared-shop",
                shop_name="共享店铺",
            ),
        ),
    )


def prepare_collected_batch(
    tmp_path: Path,
    *,
    mode: str,
    category_statuses: tuple[str, ...],
) -> tuple[Database, CollectedCategoryBatch]:
    """Create authoritative category states and their matching in-memory result."""

    # 每个测试使用独立批次目录和 SQLite 文件。
    runtime_root = tmp_path / "runtime"
    # BatchStorage 只用于提供领域对象要求的批次存储和真实 raw 路径。
    storage = BatchStorage(
        runtime_root=runtime_root,
        batch_id="batch-stage-four",
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=BATCH_STARTED_AT,
        mode=mode,  # type: ignore[arg-type]
        started_at=BATCH_STARTED_AT,
    )
    # 数据库通过正式迁移建立当前 Schema。
    database_path = runtime_root / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    database.create_batch(
        batch_id=storage.batch_id,
        task_id=storage.task_id,
        business_date=date(2026, 7, 17),
        planned_at=BATCH_STARTED_AT,
        mode=mode,
        brand_type=0,
        price_bin="10001-?",
        manifest_path=storage.manifest_path,
        started_at=BATCH_STARTED_AT,
    )
    # 分类树先落盘并在两层索引中登记。
    category_tree_path = storage.write_category_tree({"data": {"cate_list": []}})
    database.record_category_tree_raw(
        batch_id=storage.batch_id,
        category_tree_raw_path=category_tree_path,
    )
    storage.record_category_tree_saved(
        category_tree_path,
        captured_at=BATCH_STARTED_AT,
    )
    # 动态构造测试所需的成功和失败三级分类。
    categories = tuple(
        DiscoveredCategory(
            discovery_order=category_index,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id=f"level-2-{category_index}",
            level2_category_name=f"二级分类{category_index}",
            category_id=f"level-3-{category_index}",
            category_name=f"三级分类{category_index}",
        )
        for category_index in range(1, len(category_statuses) + 1)
    )
    # 分类发现结果保留所有接口顺序。
    discovery = CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=categories,
    )
    # category_run_id 跨 SQLite、raw 和内存结果保持一致。
    category_run_plans = tuple(
        CategoryRunPlan(
            category_run_id=f"category-run-{category.discovery_order}",
            category=category,
        )
        for category in categories
    )
    database.create_category_runs(
        batch_id=storage.batch_id,
        discovery=discovery,
        category_run_plans=category_run_plans,
    )
    storage.record_discovered_categories(discovery, category_run_plans)
    # 只把完整 success 分类放入阶段三返回对象。
    collected_category_runs: list[CollectedCategoryRun] = []
    for category_index, (category_status, category_run_plan) in enumerate(
        zip(category_statuses, category_run_plans, strict=True),
        start=1,
    ):
        # 每个分类使用独立时间，便于精确匹配 SQLite 生命周期。
        category_started_at = CATEGORY_STARTED_AT + timedelta(seconds=category_index)
        category_finished_at = CATEGORY_FINISHED_AT + timedelta(seconds=category_index)
        database.start_category_run(
            category_run_plan.category_run_id,
            category_started_at,
        )
        if category_status == "failed":
            # 普通失败没有成功 raw 页，failed_page 为第一页。
            database.finish_category_failure(
                category_run_plan.category_run_id,
                1,
                "request_failed",
                category_finished_at,
            )
            continue
        if category_status != "success":
            raise ValueError("test category status must be success or failed")
        # success 分类先写一页一条 raw，再完成分类状态。
        captured_at = category_started_at + timedelta(milliseconds=500)
        page_path = storage.write_category_page(
            category_run_plan.category_run_id,
            1,
            {"data": {"total": 1, "data_result": [{}]}},
        )
        raw_page = RawPageRecord(
            page_no=1,
            path=page_path,
            item_count=1,
            captured_at=captured_at,
        )
        database.record_category_page(
            category_run_plan.category_run_id,
            raw_page,
            1,
            1,
        )
        database.finish_category_success(
            category_run_plan.category_run_id,
            1,
            1,
            category_finished_at,
        )
        collected_category_runs.append(
            CollectedCategoryRun(
                plan=category_run_plan,
                started_at=category_started_at,
                finished_at=category_finished_at,
                api_total=1,
                target_page_count=1,
                raw_pages=(raw_page,),
                entries=(build_entry(captured_at=captured_at),),
            )
        )
    # 批次结果明确只包含成功分类和权威失败数量。
    collected_batch = CollectedCategoryBatch(
        batch_id=storage.batch_id,
        task_id=storage.task_id,
        business_date=date(2026, 7, 17),
        started_at=BATCH_STARTED_AT,
        finished_at=COLLECTION_FINISHED_AT,
        storage=storage,
        category_runs=tuple(collected_category_runs),
        failed_category_count=category_statuses.count("failed"),
    )
    return database, collected_batch


def prepare_staged_csv(tmp_path: Path, *, name: str = "result.csv") -> StagedCsvExport:
    """Create one complete temporary CSV owned by a publication attempt."""

    # 暂存文件和最终文件位于同一目录，匹配真实原子替换语义。
    final_path = tmp_path / "exports" / name
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.with_name(f".{final_path.name}.tmp")
    temporary_path.write_text("分类,商品\n", encoding="utf-8")
    return StagedCsvExport(
        temporary_path=temporary_path,
        final_path=final_path,
    )


def test_dry_run_finalizes_without_official_rows_or_csv(
    tmp_path: Path,
) -> None:
    """Finish a dry-run using category state while keeping it unofficial."""

    # 本用例验证零失败 success。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="dry_run",
        category_statuses=("success",),
    )
    try:
        snapshot = database.finalize_dry_run(
            collected_batch,
            COLLECTION_FINISHED_AT,
        )
        with database.session_factory() as session:
            # dry-run 不得写入任何正式商品或店铺记录。
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
            shop_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryShopModel)
            )
        # published_at 为空，因此幂等查询不能把 dry-run 当正式发布。
        published_batch = database.successful_batch(
            collected_batch.task_id,
            BATCH_STARTED_AT,
        )
    finally:
        database.close()

    assert snapshot.status == "success"
    assert snapshot.version is None
    assert snapshot.csv_path is None
    assert snapshot.published_at is None
    assert product_count == 0
    assert shop_count == 0
    assert published_batch is None


def test_dry_run_derives_partial_success_from_sqlite(tmp_path: Path) -> None:
    """Finalize one-success one-failure dry-run as partial_success."""

    # 内存失败计数必须与 SQLite 的 failed 分类完全一致。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="dry_run",
        category_statuses=("success", "failed"),
    )
    try:
        snapshot = database.finalize_dry_run(
            collected_batch,
            COLLECTION_FINISHED_AT,
        )
    finally:
        database.close()

    assert snapshot.status == "partial_success"
    assert snapshot.successful_category_count == 1
    assert snapshot.failed_category_count == 1
    assert snapshot.published_at is None


def test_official_publish_allows_same_product_and_rank_across_categories(
    tmp_path: Path,
) -> None:
    """Write scoped product uniqueness, shops, CSV, and official publication time."""

    # 两个 success 分类故意拥有相同商品 ID 和分类内 rank。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=("success", "success"),
    )
    # 实际 StagedCsvExport 验证生产文件适配器的原子发布。
    staged_csv = prepare_staged_csv(tmp_path)
    try:
        result = database.publish_collected_batch(
            collected_batch,
            1,
            staged_csv,
            PUBLISHED_AT,
        )
        with database.session_factory() as session:
            # 两个分类各写一条共享商品，不能触发跨分类唯一冲突。
            product_rows = session.scalars(
                select(ProductRankEntryModel).order_by(
                    ProductRankEntryModel.category_run_id
                )
            ).all()
            shop_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryShopModel)
            )
        # 只有 published_at 非空的批次能被正式幂等查询命中。
        published_batch = database.successful_batch(
            collected_batch.task_id,
            BATCH_STARTED_AT,
        )
    finally:
        database.close()

    assert staged_csv.final_path.exists()
    assert not staged_csv.temporary_path.exists()
    assert result.snapshot.status == "success"
    assert result.snapshot.published_at == PUBLISHED_AT
    assert result.published_batch.version == 1
    assert result.published_batch.csv_path == staged_csv.final_path
    assert [(row.product_id, row.rank) for row in product_rows] == [
        ("shared-product", 1),
        ("shared-product", 1),
    ]
    assert shop_count == 2
    assert published_batch is not None
    assert published_batch.batch_id == collected_batch.batch_id


def test_partial_success_publishes_only_successful_categories(tmp_path: Path) -> None:
    """Publish one successful category while retaining one failed category summary."""

    # 阶段三返回对象不含失败分类的任何商品残片。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=("success", "failed"),
    )
    staged_csv = prepare_staged_csv(tmp_path, name="partial.csv")
    try:
        result = database.publish_collected_batch(
            collected_batch,
            1,
            staged_csv,
            PUBLISHED_AT,
        )
        with database.session_factory() as session:
            # 只有 success 分类的一条商品进入正式表。
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()

    assert result.snapshot.status == "partial_success"
    assert result.snapshot.successful_category_count == 1
    assert result.snapshot.failed_category_count == 1
    assert product_count == 1


class PublishThenFailCsv:
    """Publish the final file and then raise to exercise filesystem compensation."""

    def __init__(self, temporary_path: Path, final_path: Path) -> None:
        """Store owned paths and initial unpublished state."""

        # 两个路径与真实 StagedCsvExport 具有相同结构。
        self.temporary_path = temporary_path
        self.final_path = final_path
        # published 控制 rollback 是否删除已移动的正式文件。
        self.published = False

    def publish(self) -> None:
        """Move the file successfully before simulating a later filesystem error."""

        os.replace(self.temporary_path, self.final_path)
        self.published = True
        raise OSError("simulated failure after atomic CSV publish")

    def rollback(self) -> None:
        """Remove temporary or final files owned by this attempt."""

        if self.temporary_path.exists():
            self.temporary_path.unlink()
        if self.published and self.final_path.exists():
            self.final_path.unlink()


class PublishThenInterruptCsv:
    """Publish the final file and then simulate Ctrl-C inside the transaction."""

    def __init__(self, temporary_path: Path, final_path: Path) -> None:
        """Store owned paths and initial publication state."""

        # 两个路径与真实 StagedCsvExport 具有相同结构。
        self.temporary_path = temporary_path
        self.final_path = final_path
        # published 控制 rollback 是否删除已经移动的正式文件。
        self.published = False

    def publish(self) -> None:
        """Move the file before raising a BaseException-derived interruption."""

        os.replace(self.temporary_path, self.final_path)
        self.published = True
        raise KeyboardInterrupt

    def rollback(self) -> None:
        """Remove temporary or final files owned by this interrupted attempt."""

        if self.temporary_path.exists():
            self.temporary_path.unlink()
        if self.published and self.final_path.exists():
            self.final_path.unlink()


class RollbackAlwaysFailsCsv:
    """Fail publication cleanup without exposing its filesystem error text."""

    def __init__(self, temporary_path: Path, final_path: Path) -> None:
        """Store the staged paths required by the publication protocol."""

        # 临时路径指向测试准备的未发布 CSV。
        self.temporary_path = temporary_path
        # 最终路径用于满足正式发布协议但不会实际生成文件。
        self.final_path = final_path

    def publish(self) -> None:
        """Raise one unsafe internal error before publishing the CSV."""

        raise OSError("unsafe publish filesystem detail")

    def rollback(self) -> None:
        """Raise one unsafe internal cleanup error for safe wrapping assertions."""

        raise OSError("unsafe rollback filesystem detail")


def test_publish_failure_rolls_back_csv_product_rows_and_batch_state(
    tmp_path: Path,
) -> None:
    """Compensate an already-moved CSV and rollback all flushed SQLite rows."""

    # 一条 success 商品会在 CSV publish 前完成事务内 flush。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=("success",),
    )
    # 自定义 staged 对象在原子移动成功后抛错。
    normal_staged_csv = prepare_staged_csv(tmp_path, name="rollback.csv")
    staged_csv = PublishThenFailCsv(
        normal_staged_csv.temporary_path,
        normal_staged_csv.final_path,
    )
    try:
        with pytest.raises(PublicationError) as error_info:
            database.publish_collected_batch(
                collected_batch,
                1,
                staged_csv,
                PUBLISHED_AT,
            )
        with database.session_factory() as session:
            # 事务回滚后批次恢复 running 且不占用版本或 CSV 路径。
            batch = session.get(CollectionBatch, collected_batch.batch_id)
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
            shop_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryShopModel)
            )
    finally:
        database.close()

    assert error_info.value.category == "publication_failed"
    assert batch is not None
    assert batch.status == "running"
    assert batch.version is None
    assert batch.csv_path is None
    assert batch.published_at is None
    assert product_count == 0
    assert shop_count == 0
    assert not staged_csv.temporary_path.exists()
    assert not staged_csv.final_path.exists()


def test_publish_keyboard_interrupt_rolls_back_csv_and_database(
    tmp_path: Path,
) -> None:
    """Compensate an already-published CSV before preserving Ctrl-C semantics."""

    # 一条成功分类让发布路径在中止前 flush 正式商品行。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=("success",),
    )
    # 自定义 staged 对象在正式文件移动成功后触发 KeyboardInterrupt。
    normal_staged_csv = prepare_staged_csv(tmp_path, name="interrupt.csv")
    staged_csv = PublishThenInterruptCsv(
        normal_staged_csv.temporary_path,
        normal_staged_csv.final_path,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            database.publish_collected_batch(
                collected_batch,
                1,
                staged_csv,
                PUBLISHED_AT,
            )
        with database.session_factory() as session:
            # BaseException 也必须让事务恢复为可由 runner 收口的 running 状态。
            batch = session.get(CollectionBatch, collected_batch.batch_id)
            # 正式商品计数证明 flush 结果没有被部分提交。
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()

    assert batch is not None
    assert batch.status == "running"
    assert batch.version is None
    assert batch.csv_path is None
    assert batch.published_at is None
    assert product_count == 0
    assert not staged_csv.temporary_path.exists()
    assert not staged_csv.final_path.exists()


@pytest.mark.parametrize(
    ("category_statuses", "expected_status", "interruption"),
    (
        (("success",), "success", KeyboardInterrupt()),
        (("success", "failed"), "partial_success", SystemExit(23)),
    ),
    ids=("success-keyboard-interrupt", "partial-success-system-exit"),
)
def test_publish_boundary_interruption_preserves_committed_csv_and_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category_statuses: tuple[str, ...],
    expected_status: str,
    interruption: BaseException,
) -> None:
    """Preserve a committed CSV when begin exits with a process interruption."""

    # 成功与部分成功分别验证两个正式 SQLite 终态都能保护已发布 CSV。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=category_statuses,
    )
    # 真实 staged 对象确保 commit 前已执行最终文件原子移动。
    staged_csv = prepare_staged_csv(tmp_path, name="committed-interruption.csv")
    # 保存真实 begin，包装层必须先让其完整退出并完成数据库 commit。
    original_begin = database.session_factory.begin

    @contextmanager
    def begin_then_interrupt_after_commit() -> Iterator[Session]:
        """Raise only after the real transaction context has committed."""

        with original_begin() as session:
            yield session
        raise interruption

    # 中止由 publish 使用的 begin 上下文退出边界触发，不在调用方外层伪造。
    monkeypatch.setattr(
        database.session_factory,
        "begin",
        begin_then_interrupt_after_commit,
    )
    # 原始异常类型与对象都必须穿过发布补偿层保持不变。
    expected_error_type = type(interruption)
    try:
        with pytest.raises(expected_error_type) as error_info:
            database.publish_collected_batch(
                collected_batch,
                1,
                staged_csv,
                PUBLISHED_AT,
            )
        with database.session_factory() as session:
            # 新 Session 读取成功证明断言观察的是已提交状态而非 identity map。
            batch = session.get(CollectionBatch, collected_batch.batch_id)
            # 正式商品计数证明商品事务也随批次终态完成提交。
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()

    assert error_info.value is interruption
    assert batch is not None
    assert batch.status == expected_status
    assert batch.version == 1
    assert batch.csv_path == str(staged_csv.final_path)
    assert batch.published_at == PUBLISHED_AT
    assert product_count == 1
    assert not staged_csv.temporary_path.exists()
    assert staged_csv.final_path.exists()


def test_invalid_version_surfaces_safe_csv_cleanup_failure(tmp_path: Path) -> None:
    """Prefer the stable cleanup category when invalid-version cleanup also fails."""

    # 合法采集批次证明错误发生在版本预检而不是数据库校验。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=("success",),
    )
    # 正常暂存文件提供真实且独立的临时和最终路径。
    normal_staged_csv = prepare_staged_csv(tmp_path, name="invalid-version.csv")
    # 包装对象只在 rollback 阶段失败，publish 不应被非法版本分支调用。
    staged_csv = RollbackAlwaysFailsCsv(
        normal_staged_csv.temporary_path,
        normal_staged_csv.final_path,
    )
    try:
        with pytest.raises(PublicationError) as error_info:
            database.publish_collected_batch(
                collected_batch,
                0,
                staged_csv,
                PUBLISHED_AT,
            )
    finally:
        database.close()

    assert error_info.value.category == "publication_cleanup_failed"
    assert str(error_info.value) == "failed to clean up staged CSV"
    assert "unsafe rollback filesystem detail" not in str(error_info.value)


def test_publish_error_surfaces_safe_csv_cleanup_failure(tmp_path: Path) -> None:
    """Surface cleanup failure when compensating a regular publication exception."""

    # 单个成功分类会让执行路径进入数据库发布事务和 CSV publish。
    database, collected_batch = prepare_collected_batch(
        tmp_path,
        mode="normal",
        category_statuses=("success",),
    )
    # 正常暂存文件提供生产协议要求的两个路径。
    normal_staged_csv = prepare_staged_csv(tmp_path, name="cleanup-failure.csv")
    # 包装对象先让 publish 失败，再让补偿 rollback 失败。
    staged_csv = RollbackAlwaysFailsCsv(
        normal_staged_csv.temporary_path,
        normal_staged_csv.final_path,
    )
    try:
        with pytest.raises(PublicationError) as error_info:
            database.publish_collected_batch(
                collected_batch,
                1,
                staged_csv,
                PUBLISHED_AT,
            )
        with database.session_factory() as session:
            # 清理失败不能阻止 SQLite 事务回滚为采集完成前状态。
            batch = session.get(CollectionBatch, collected_batch.batch_id)
            # 正式商品计数用于证明发布事务没有部分提交。
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()

    assert error_info.value.category == "publication_cleanup_failed"
    assert str(error_info.value) == "failed to clean up staged CSV"
    assert "unsafe publish filesystem detail" not in str(error_info.value)
    assert "unsafe rollback filesystem detail" not in str(error_info.value)
    assert batch is not None
    assert batch.status == "running"
    assert batch.version is None
    assert batch.csv_path is None
    assert batch.published_at is None
    assert product_count == 0


def test_rejects_no_success_or_missing_success_result_and_cleans_staging(
    tmp_path: Path,
) -> None:
    """Reject zero-success batches and an in-memory result missing a success category."""

    # 两个 failed 分类没有任何可发布成功分类。
    failed_database, failed_batch = prepare_collected_batch(
        tmp_path / "no-success",
        mode="normal",
        category_statuses=("failed", "failed"),
    )
    failed_staged_csv = prepare_staged_csv(tmp_path / "no-success")
    try:
        with pytest.raises(PublicationError, match="no successful category"):
            failed_database.publish_collected_batch(
                failed_batch,
                1,
                failed_staged_csv,
                PUBLISHED_AT,
            )
    finally:
        failed_database.close()
    # 两个 SQLite success 分类但内存故意遗漏第二分类。
    missing_database, complete_batch = prepare_collected_batch(
        tmp_path / "missing-success",
        mode="normal",
        category_statuses=("success", "success"),
    )
    missing_batch = replace(
        complete_batch,
        category_runs=complete_batch.category_runs[:1],
    )
    missing_staged_csv = prepare_staged_csv(tmp_path / "missing-success")
    try:
        with pytest.raises(PublicationError, match="all successful categories"):
            missing_database.publish_collected_batch(
                missing_batch,
                1,
                missing_staged_csv,
                PUBLISHED_AT,
            )
    finally:
        missing_database.close()

    assert not failed_staged_csv.temporary_path.exists()
    assert not failed_staged_csv.final_path.exists()
    assert not missing_staged_csv.temporary_path.exists()
    assert not missing_staged_csv.final_path.exists()
