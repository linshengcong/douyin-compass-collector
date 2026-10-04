"""Immutable domain records shared by parsing, persistence, and CSV export."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from compass_collector.raw_storage import BatchStorage


@dataclass(frozen=True, slots=True)
class MetricRange:
    """Preserve one platform value range without persistence-layer conversion."""

    min_value: Decimal
    max_value: Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class ProductShop:
    """Preserve a product-shop relationship and its source order."""

    position: int
    # 淘宝只确认卖家用户标识；未知的真实店铺 ID 保留为空。
    shop_id: str | None
    shop_name: str
    # 店铺链接和卖家用户标识分别保存，不能混作店铺 ID。
    shop_url: str | None = None
    seller_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProductRankEntry:
    """Represent one validated product ranking row."""

    page_no: int
    captured_at: datetime
    rank: int
    product_id: str
    product_name: str
    # 平台专属字段允许不适用；各平台解析器仍验证自己必需的指标。
    newly_on_ranking: bool | None
    pay_amount: MetricRange | None
    pay_combo_count: MetricRange | None
    shops: tuple[ProductShop, ...]
    # image_url 是平台商品图片地址；旧响应缺失时保留空值以兼容历史快照。
    image_url: str | None = None
    # 淘宝返回的商品跳转链接保持原始语义，不由商品 ID 猜测。
    product_url: str | None = None
    # 淘宝人数指标同时保存原文和可空范围，缺失不能转成零。
    pay_buyer_count_raw: str | None = None
    pay_buyer_count: MetricRange | None = None
    visitor_count_raw: str | None = None
    visitor_count: MetricRange | None = None


@dataclass(frozen=True, slots=True)
class RawPageRecord:
    """Describe one validated raw response file for database indexing."""

    page_no: int
    path: Path
    item_count: int
    captured_at: datetime
    # 只保存适配器提供的安全业务参数，不保存签名或认证。
    safe_params: dict[str, str | int] = field(default_factory=dict)


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

    @property
    def key(self) -> str:
        """Expose the legacy category identity for migration compatibility."""
        return self.category_id

    @property
    def path(self) -> tuple[str, ...]:
        """Read legacy three-level paths through the shared scope contract."""
        return (
            self.level1_category_name,
            self.level2_category_name,
            self.category_name,
        )

    @property
    def platform_metadata(self) -> dict[str, str]:
        """Keep legacy Compass IDs available during historical migration."""
        return {
            "industry_id": self.level1_category_id,
            "level2_id": self.level2_category_id,
            "category_id": self.category_id,
        }


@dataclass(frozen=True, slots=True)
class DiscoveredScope:
    """Generic scope identity, ordered path and adapter-owned metadata."""

    # 任意平台可以提供一层或多层路径，不要求三级类目。
    discovery_order: int
    key: str
    path: tuple[str, ...]
    platform_metadata: dict[str, str]

    @property
    def display_path(self) -> str:
        """Format the complete generic path without dropping deeper levels."""
        return " > ".join(self.path)

    @property
    def category_id(self) -> str:
        """Expose the persisted identity used by existing category-run indexes."""
        return self.key

    @property
    def category_name(self) -> str:
        """Provide the last path segment for historical readers."""
        return self.path[-1]

    @property
    def level1_category_id(self) -> str:
        """Project each platform's actual root ID into the shared category columns."""
        return self.platform_metadata.get("industry_id", self.platform_metadata.get("root_category_id", ""))

    @property
    def level2_category_id(self) -> str:
        """Project the actual second-level ID without altering platform request keys."""
        return self.platform_metadata.get("level2_id", self.platform_metadata.get("parent_cate_id", ""))

    @property
    def level1_category_name(self) -> str:
        """Retain existing website projection for Compass snapshots."""
        return self.path[0]

    @property
    def level2_category_name(self) -> str:
        """Retain an optional second segment for historical readers."""
        return self.path[1] if len(self.path) > 1 else ""


@dataclass(frozen=True, slots=True)
class CategoryDiscoveryResult:
    """Bundle one category scope and all discovered level-three categories."""

    # 多一级分类范围没有单一真实根节点，因此批次根快照保持为空。
    root_category_id: str | None
    root_category_name: str | None
    # categories 已排除“全部”并忽略四级及更深节点。
    categories: tuple[DiscoveredScope | DiscoveredCategory, ...]


@dataclass(frozen=True, slots=True)
class CategoryRunPlan:
    """Assign one stable category_run_id before database and Manifest writes."""

    # category_run_id 用于连接分页 raw、运行状态和正式排名。
    category_run_id: str
    category: DiscoveredScope | DiscoveredCategory


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
    # raw_pages 只索引已按 raw -> PostgreSQL -> Manifest 顺序保存的页面。
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
