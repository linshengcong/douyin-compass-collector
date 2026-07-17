"""Immutable domain records shared by parsing, persistence, and CSV export."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from compass_collector.raw_storage import BatchStorage


@dataclass(frozen=True, slots=True)
class MetricRange:
    """Preserve one platform value range without persistence-layer conversion."""

    min_value: int
    max_value: int
    unit: str


@dataclass(frozen=True, slots=True)
class ProductShop:
    """Preserve a product-shop relationship and its source order."""

    position: int
    shop_id: str
    shop_name: str


@dataclass(frozen=True, slots=True)
class ProductRankEntry:
    """Represent one validated product ranking row."""

    page_no: int
    captured_at: datetime
    rank: int
    product_id: str
    product_name: str
    newly_on_ranking: bool
    pay_amount: MetricRange
    pay_combo_count: MetricRange
    shops: tuple[ProductShop, ...]


@dataclass(frozen=True, slots=True)
class RawPageRecord:
    """Describe one validated raw response file for database indexing."""

    page_no: int
    path: Path
    item_count: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class DiscoveredCategory:
    """Preserve one target level-three category and its full source path."""

    # discovery_order 保留分类接口原始顺序。
    discovery_order: int
    # 一级分类同时作为榜单请求的 industry_id。
    level1_category_id: str
    level1_category_name: str
    # 二级分类只用于完整路径和 CSV 展示。
    level2_category_id: str
    level2_category_name: str
    # 三级分类是后续榜单分页的 category_id。
    category_id: str
    category_name: str

    @property
    def display_path(self) -> str:
        """Return the human-readable three-level category path."""

        return (
            f"{self.level1_category_name} > "
            f"{self.level2_category_name} > "
            f"{self.category_name}"
        )


@dataclass(frozen=True, slots=True)
class CategoryDiscoveryResult:
    """Bundle one category scope and all discovered level-three categories."""

    # 多一级分类范围没有单一真实根节点，因此批次根快照保持为空。
    root_category_id: str | None
    root_category_name: str | None
    # categories 已排除“全部”并忽略四级及更深节点。
    categories: tuple[DiscoveredCategory, ...]


@dataclass(frozen=True, slots=True)
class CategoryRunPlan:
    """Assign one stable category_run_id before database and Manifest writes."""

    # category_run_id 用于连接分页 raw、运行状态和正式排名。
    category_run_id: str
    category: DiscoveredCategory


@dataclass(frozen=True, slots=True)
class CollectedCategoryRun:
    """Bundle one fully validated level-three ranking before publication."""

    # plan 保留分类快照与跨层稳定的 category_run_id。
    plan: CategoryRunPlan
    # started_at 和 finished_at 只描述该分类的榜单采集窗口。
    started_at: datetime
    finished_at: datetime
    # api_total 与目标页数来自第一页已验证分页元数据。
    api_total: int
    target_page_count: int
    # raw_pages 只索引已按 raw -> SQLite -> Manifest 顺序保存的页面。
    raw_pages: tuple[RawPageRecord, ...]
    # entries 只包含通过完整榜单校验的商品，不暴露失败分类残片。
    entries: tuple[ProductRankEntry, ...]


@dataclass(frozen=True, slots=True)
class CollectedCategoryBatch:
    """Expose successful category snapshots after stage-three collection."""

    # batch_id 和 task_id 连接发现、采集以及后续发布阶段。
    batch_id: str
    task_id: str
    business_date: date
    # 批次时间沿用分类发现开始时间并记录采集准备完成时间。
    started_at: datetime
    finished_at: datetime
    # storage 继续交给后续发布阶段更新同一个 Manifest。
    storage: BatchStorage
    # category_runs 只保留完整成功分类，失败分类不会返回 entries。
    category_runs: tuple[CollectedCategoryRun, ...]
    # failed_category_count 统计全部跳过的分类，用于部分成功发布汇总。
    failed_category_count: int
