"""CSV presentation and atomic staging tests for category batches."""

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from compass_collector.errors import PublicationError
from compass_collector.exporter import CSV_HEADERS, CsvExporter
from compass_collector.models import (
    CategoryRunPlan,
    CollectedCategoryRun,
    DiscoveredCategory,
    MetricRange,
    ProductRankEntry,
    ProductShop,
)


def build_entry(*, rank: int, product_name: str, newly_on_ranking: bool) -> ProductRankEntry:
    """Build one deterministic product row for CSV assertions."""

    # 固定捕获时间避免测试数据引入无关差异。
    captured_at = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
    # 两家店铺的顺序用于验证导出器不会自行重排。
    shops = (
        ProductShop(position=2, shop_id="shop-2", shop_name="店铺乙"),
        ProductShop(position=1, shop_id="shop-1", shop_name="店铺甲"),
    )
    return ProductRankEntry(
        page_no=1,
        captured_at=captured_at,
        rank=rank,
        product_id=f"product-{product_name}",
        product_name=product_name,
        newly_on_ranking=newly_on_ranking,
        pay_amount=MetricRange(min_value=100_000, max_value=250_000, unit="price"),
        pay_combo_count=MetricRange(min_value=100, max_value=250, unit="number"),
        shops=shops,
        image_url=f"https://images.example.test/{product_name}.jpg",
    )


def build_category_run(
    *,
    discovery_order: int,
    level2_name: str,
    category_name: str,
    entries: tuple[ProductRankEntry, ...],
) -> CollectedCategoryRun:
    """Build one successful category result without raw-page dependencies."""

    # 分类标识包含发现顺序，确保不同测试分类互不混淆。
    category_suffix = str(discovery_order)
    # 分类对象保留完整三级路径和接口发现顺序。
    category = DiscoveredCategory(
        discovery_order=discovery_order,
        level1_category_id="13",
        level1_category_name="食品饮料",
        level2_category_id=f"level-2-{category_suffix}",
        level2_category_name=level2_name,
        category_id=f"level-3-{category_suffix}",
        category_name=category_name,
    )
    # 运行计划将分类快照连接到稳定的分类运行标识。
    plan = CategoryRunPlan(
        category_run_id=f"category-run-{category_suffix}",
        category=category,
    )
    # 固定运行时间仅用于满足领域对象完整契约。
    run_time = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
    return CollectedCategoryRun(
        plan=plan,
        started_at=run_time,
        finished_at=run_time,
        api_total=len(entries),
        target_page_count=1,
        raw_pages=(),
        entries=entries,
    )


def read_csv_rows(path: Path) -> list[list[str]]:
    """Read one staged CSV through its UTF-8 BOM contract."""

    # utf-8-sig 在读取时移除 BOM，便于直接断言中文表头。
    with path.open("r", encoding="utf-8-sig", newline="") as file_handle:
        return list(csv.reader(file_handle))


def test_prepare_exports_categories_in_discovery_and_rank_order(tmp_path: Path) -> None:
    """Export complete category paths and product rows in stable business order."""

    # 后发现分类故意先传入，且分类内排名也故意打乱。
    later_category = build_category_run(
        discovery_order=2,
        level2_name="冲调饮品",
        category_name="咖啡",
        entries=(
            build_entry(rank=2, product_name="咖啡二号", newly_on_ranking=False),
            build_entry(rank=1, product_name="咖啡一号", newly_on_ranking=True),
        ),
    )
    # 先发现分类用于验证它会排到 CSV 最前面。
    earlier_category = build_category_run(
        discovery_order=1,
        level2_name="粮油干货/方便速食",
        category_name="食用油",
        entries=(
            build_entry(rank=2, product_name="食用油二号", newly_on_ranking=False),
            build_entry(rank=1, product_name="食用油一号", newly_on_ranking=True),
        ),
    )
    # 导出器只负责把成功分类结果写入临时 CSV。
    staged_csv = CsvExporter(tmp_path).prepare(
        task_id="product_hot_sale_food_level3",
        display_name="食品饮料三级分类商品实时榜",
        planned_at=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
        version=1,
        batch_id="batch-order",
        category_runs=(later_category, earlier_category),
    )

    # 原始字节必须以 UTF-8 BOM 开始，兼容常用表格软件。
    assert staged_csv.temporary_path.read_bytes().startswith(b"\xef\xbb\xbf")
    # CSV 行用于验证表头、完整分类路径和稳定排序。
    rows = read_csv_rows(staged_csv.temporary_path)
    assert rows[0] == list(CSV_HEADERS)
    assert rows[1] == [
        "食品饮料 > 粮油干货/方便速食 > 食用油",
        "1",
        "https://images.example.test/食用油一号.jpg",
        "食用油一号",
        "店铺乙 | 店铺甲",
        "¥1000-¥2500",
        "10-25",
        "true",
    ]
    assert [(row[0], row[1], row[2], row[3], row[7]) for row in rows[1:]] == [
        ("食品饮料 > 粮油干货/方便速食 > 食用油", "1", "https://images.example.test/食用油一号.jpg", "食用油一号", "true"),
        ("食品饮料 > 粮油干货/方便速食 > 食用油", "2", "https://images.example.test/食用油二号.jpg", "食用油二号", "false"),
        ("食品饮料 > 冲调饮品 > 咖啡", "1", "https://images.example.test/咖啡一号.jpg", "咖啡一号", "true"),
        ("食品饮料 > 冲调饮品 > 咖啡", "2", "https://images.example.test/咖啡二号.jpg", "咖啡二号", "false"),
    ]


def test_prepare_uses_chinese_name_version_and_safe_path(tmp_path: Path) -> None:
    """Keep version semantics while preventing display names from creating paths."""

    # 空分类批次仍应生成只有表头的合法 CSV。
    exporter = CsvExporter(tmp_path)
    # v1 文件不带版本后缀，展示名中的两类分隔符都要替换。
    version_one = exporter.prepare(
        task_id="product_hot_sale_food_level3",
        display_name="食品/饮料\\三级榜",
        planned_at=datetime(2026, 7, 17, 14, 5, tzinfo=timezone.utc),
        version=1,
        batch_id="batch-v1",
        category_runs=(),
    )
    # v2 文件从第二版开始显式携带版本后缀。
    version_two = exporter.prepare(
        task_id="product_hot_sale_food_level3",
        display_name="食品/饮料\\三级榜",
        planned_at=datetime(2026, 7, 17, 14, 5, tzinfo=timezone.utc),
        version=2,
        batch_id="batch-v2",
        category_runs=(),
    )

    # 两个文件必须位于同一个业务日期和任务隔离目录下。
    expected_directory = (
        tmp_path / "2026-07-17" / "product_hot_sale_food_level3"
    )
    assert version_one.final_path == expected_directory / "食品_饮料_三级榜_1405.csv"
    assert version_two.final_path == expected_directory / "食品_饮料_三级榜_1405_v2.csv"
    assert version_one.temporary_path.name == ".食品_饮料_三级榜_1405.csv.batch-v1.tmp"
    assert version_two.temporary_path.name == ".食品_饮料_三级榜_1405_v2.csv.batch-v2.tmp"
    assert read_csv_rows(version_one.temporary_path) == [list(CSV_HEADERS)]


def test_staged_export_preserves_publish_and_rollback_atomic_behavior(tmp_path: Path) -> None:
    """Publish by atomic move and remove the owned final file on rollback."""

    # 单分类结果足以验证暂存文件的完整生命周期。
    category_run = build_category_run(
        discovery_order=1,
        level2_name="休闲食品",
        category_name="肉干肉脯",
        entries=(build_entry(rank=1, product_name="牛肉干", newly_on_ranking=False),),
    )
    # 发布前只允许临时文件存在。
    staged_csv = CsvExporter(tmp_path).prepare(
        task_id="product_hot_sale_food_level3",
        display_name="食品饮料三级分类商品实时榜",
        planned_at=datetime(2026, 7, 17, 14, 10, tzinfo=timezone.utc),
        version=1,
        batch_id="batch-publish",
        category_runs=(category_run,),
    )
    assert staged_csv.temporary_path.exists()
    assert not staged_csv.final_path.exists()

    # publish 使用原子替换把完整临时文件变为正式文件。
    staged_csv.publish()
    assert staged_csv.published is True
    assert not staged_csv.temporary_path.exists()
    assert staged_csv.final_path.exists()

    # rollback 只删除本次暂存对象已经发布的正式文件。
    staged_csv.rollback()
    assert not staged_csv.final_path.exists()


@pytest.mark.parametrize(
    "task_id",
    ("../escape", "task/name", "", "TaskUpper"),
)
def test_prepare_rejects_unsafe_task_id(tmp_path: Path, task_id: str) -> None:
    """Reject any task ID that cannot be one strict directory segment."""

    # 非法任务 ID 必须在创建日期目录或临时文件之前被拒绝。
    with pytest.raises(PublicationError) as error_info:
        CsvExporter(tmp_path).prepare(
            task_id=task_id,
            display_name="食品饮料三级分类商品实时榜",
            planned_at=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
            version=1,
            batch_id="batch-invalid-task-id",
            category_runs=(),
        )

    assert error_info.value.category == "csv_path_error"
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    "interruption",
    (KeyboardInterrupt(), SystemExit(23)),
    ids=("keyboard-interrupt", "system-exit"),
)
def test_prepare_removes_temporary_csv_and_preserves_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    """Remove the owned temporary CSV before preserving process interruption."""

    def interrupt_csv_writer(*_args: object, **_kwargs: object) -> None:
        """Interrupt CSV construction after the temporary file has been opened."""

        raise interruption

    # 替换 writer 构造点，确保中止发生在临时文件已经由本次 prepare 创建之后。
    monkeypatch.setattr("compass_collector.exporter.csv.writer", interrupt_csv_writer)
    # 原始异常对象用于验证导出器没有转换进程级中止语义。
    expected_error_type = type(interruption)
    with pytest.raises(expected_error_type) as error_info:
        CsvExporter(tmp_path).prepare(
            task_id="product_hot_sale_food_level3",
            display_name="食品饮料三级分类商品实时榜",
            planned_at=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
            version=1,
            batch_id="batch-interrupted",
            category_runs=(),
        )

    assert error_info.value is interruption
    assert list(tmp_path.rglob("*.tmp")) == []


def test_prepare_removes_temporary_csv_and_wraps_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ordinary CSV write failures behind the safe publication error."""

    def fail_csv_writer(*_args: object, **_kwargs: object) -> None:
        """Raise one unsafe filesystem detail after opening the temporary CSV."""

        raise OSError("unsafe local CSV detail")

    # 普通 Exception 与中止信号走同一补偿入口，但必须继续转换为安全错误。
    monkeypatch.setattr("compass_collector.exporter.csv.writer", fail_csv_writer)
    with pytest.raises(PublicationError) as error_info:
        CsvExporter(tmp_path).prepare(
            task_id="product_hot_sale_food_level3",
            display_name="食品饮料三级分类商品实时榜",
            planned_at=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
            version=1,
            batch_id="batch-write-error",
            category_runs=(),
        )

    assert error_info.value.category == "csv_write_error"
    assert "unsafe local CSV detail" not in str(error_info.value)
    assert list(tmp_path.rglob("*.tmp")) == []
