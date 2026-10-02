"""Explicit Taobao business configuration; shared execution stays in config.py."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaobaoStrictModel(BaseModel):
    """Reject unobserved or misspelled options before opening a browser."""

    # 未知字段不静默降级为另一个平台的条件。
    model_config = ConfigDict(extra="forbid")


class TaobaoCategoryScopeConfig(TaobaoStrictModel):
    """Discover ordered third-level descendants within one root."""

    # 一级根与指定三级 ID 分开保存，不使用抖音的逗号分类编码。
    mode: Literal["all_level1", "selected"] = "all_level1"
    root_category_id: str = Field(default="50025705", pattern=r"^[1-9][0-9]*$")
    targets: list[str] = Field(default_factory=list)
    # 第一版只采三级并排除汇总分类。
    target_level: Literal[3] = 3
    exclude_all: Literal[True] = True

    @model_validator(mode="after")
    def validate_selection(self):
        """Require unique positive IDs only in selected mode."""
        if (self.mode == "selected") != bool(self.targets):
            raise ValueError("selected requires targets; all_level1 forbids targets")
        if any(not value.isascii() or not value.isdecimal() or value.startswith("0") for value in self.targets):
            raise ValueError("Taobao targets must be positive category IDs")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("duplicate Taobao category target")
        return self


class TaobaoRankConfig(TaobaoStrictModel):
    """Only expose the observed real-time GMV ranking and twenty-row paging."""

    # 接口路径只用于匹配页面响应，不用于独立 HTTP 请求。
    type: Literal["product_market_rank"] = "product_market_rank"
    endpoint_path: Literal["/mc/mq/mkt/item/live/rank.json"] = (
        "/mc/mq/mkt/item/live/rank.json"
    )
    rank_type: Literal["gmv"] = "gmv"
    page_size: Literal[20] = 20


class TaobaoFiltersConfig(TaobaoStrictModel):
    """Preserve the observed unrestricted filters without invented options."""

    # 空价格与关键词、全部店铺是已观察到的页面业务条件。
    min_price: Literal[""] = ""
    max_price: Literal[""] = ""
    price_segment: Literal[""] = ""
    seller_type: Literal[-1] = -1
    keyword: Literal[""] = ""


class TaobaoDateConfig(TaobaoStrictModel):
    """Use the task's Beijing business date in the current-day UI mode."""

    # today 是淘宝参数，不能与抖音的数值 date_type 混用。
    strategy: Literal["today"] = "today"
    date_type: Literal["today"] = "today"
