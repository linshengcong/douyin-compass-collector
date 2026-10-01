"""Compass-specific category, ranking and filter configuration contracts."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompassStrictModel(BaseModel):
    """Reject unknown Compass options before entering the browser."""

    # 平台只接收明确支持的业务条件，未知字段不静默忽略。
    model_config = ConfigDict(extra="forbid")


class CategoryTargetConfig(CompassStrictModel):
    """Identify one verified Compass level-three category."""

    # 参数按字符串保留，不允许路径或任意接口表达式。
    industry_id: str = Field(pattern=r"^[1-9][0-9]*$")
    category_id: str = Field(pattern=r"^[1-9][0-9]*,[1-9][0-9]*$")


class CategoryScopeConfig(CompassStrictModel):
    """Discover third-level categories within all or one configured industry."""

    # 自动发现可遍历全部行业，或使用 industry_id 限定一个行业。
    mode: Literal["all_level1", "selected"] = "all_level1"
    # 自动发现可限定到一个行业；未设置时保留遍历全部行业的兼容行为。
    industry_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    # 指定列表的执行顺序就是配置顺序。
    targets: list[CategoryTargetConfig] = Field(default_factory=list)
    # 当前数据契约只采集三级分类。
    target_level: Literal[3] = 3
    # “全部”节点必须排除，避免与子分类重复。
    exclude_all: Literal[True] = True

    @model_validator(mode="after")
    def validate_targets(self):
        """Reject empty selected lists, duplicates and contradictory modes."""
        if (self.mode == "selected") != bool(self.targets):
            raise ValueError("selected requires targets; all_level1 forbids targets")
        if self.mode == "selected" and self.industry_id is not None:
            raise ValueError("selected industry IDs belong in targets")
        # 完整组合唯一，避免同分类被重复采集和发布。
        identities = [
            (target.industry_id, target.category_id) for target in self.targets
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate category target")
        return self


class FiltersConfig(CompassStrictModel):
    """Configure the verified product ranking filters."""

    # 当前只开放真实请求验证过的不限和非知名品牌值。
    brand_type: Literal[-1, 0] = -1
    # 当前只开放真实请求验证过的不限和严格大于一万元价格带。
    price_bin: Literal["不限", "10001-?"] = "不限"
    search_info: Literal[""] = ""


class RankConfig(CompassStrictModel):
    """Configure the verified product hot-sale endpoint."""

    type: Literal["product_hot_sale"] = "product_hot_sale"
    endpoint_path: Literal["/compass_api/shop/product/product_rank/market_hot_sale"] = (
        "/compass_api/shop/product/product_rank/market_hot_sale"
    )
    rank_data_type: Literal[1] = 1
    activity_id: Literal[""] = ""


class DateConfig(CompassStrictModel):
    """Restrict collection to the verified current-day request semantics."""

    strategy: Literal["today"] = "today"
    date_type: Literal[1] = 1
