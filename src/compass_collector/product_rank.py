"""Verified request and response contract for the product hot-sale ranking."""

from dataclasses import dataclass
from datetime import date, datetime
from math import ceil
from typing import Any

from compass_collector.config import TaskConfig
from compass_collector.errors import ResponseContractError
from compass_collector.models import (
    DiscoveredCategory,
    MetricRange,
    ProductRankEntry,
    ProductShop,
)


# 真实接口已验证每页固定返回最多 10 条。
PAGE_SIZE = 10


@dataclass(frozen=True, slots=True)
class PaginationPlan:
    """Describe the complete uncapped pagination required by one category."""

    # api_total 保留平台第一页返回的完整榜单条数。
    api_total: int
    # target_page_count 至少为 1，空榜单也保留一次真实首页请求。
    target_page_count: int


@dataclass(frozen=True, slots=True)
class PageContract:
    """Expose the validated fields needed by the collection loop."""

    # api_total 在同一个三级分类的全部分页中必须保持稳定。
    api_total: int
    # target_page_count 由完整 total 和固定 page_size=10 推导。
    target_page_count: int
    # item_count 是当前页已经通过条数契约的实际行数。
    item_count: int


def calculate_pagination_plan(total: int) -> PaginationPlan:
    """Calculate every required ten-item page without applying an item cap."""

    # bool 是 int 的子类，必须显式拒绝以免 False 被当成空榜单。
    if type(total) is not int or total < 0:
        raise ValueError("total must be a non-negative integer")
    # 空榜单仍请求第一页；正数榜单按十条一页完整向上取整。
    target_page_count = max(1, ceil(total / PAGE_SIZE))
    return PaginationPlan(
        api_total=total,
        target_page_count=target_page_count,
    )


def build_request_params(
    task: TaskConfig,
    category: DiscoveredCategory,
    business_date: date,
    page_no: int,
) -> dict[str, str | int]:
    """Build only the verified business query parameters."""

    # 页码必须从 1 开始，bool 不能冒充整数页码。
    if type(page_no) is not int or page_no < 1:
        raise ValueError("page_no must be a positive integer")
    # 平台日期参数在任务启动时固定，全部分页共用。
    platform_date = business_date.strftime("%Y/%m/%d 00:00:00")
    return {
        "page_no": page_no,
        "page_size": PAGE_SIZE,
        "industry_id": category.level1_category_id,
        # 级联选择器要求二级与三级 ID 按页面请求格式共同提交。
        "category_id": f"{category.level2_category_id},{category.category_id}",
        "brand_type": task.filters.brand_type,
        "price_bin": task.filters.price_bin,
        "search_info": task.filters.search_info,
        "rank_data_type": task.rank.rank_data_type,
        "begin_date": platform_date,
        "end_date": platform_date,
        "date_type": task.date.date_type,
        "activity_id": task.rank.activity_id,
    }


def validate_page_payload(
    payload: dict[str, Any],
    requested_page: int,
    expected_total: int | None,
) -> PageContract:
    """Validate the real response contract before the page is persisted."""

    # 请求页码由采集循环生成，但契约函数仍拒绝 bool、0 和负数。
    if type(requested_page) is not int or requested_page < 1:
        raise ValueError("requested_page must be a positive integer")
    # 后续页携带的首屏 total 也必须保持严格非负整数语义。
    if expected_total is not None and (
        type(expected_total) is not int or expected_total < 0
    ):
        raise ValueError("expected_total must be a non-negative integer")
    # 解码后的 JSON 根必须是对象，避免非对象响应触发普通 AttributeError。
    if not isinstance(payload, dict):
        raise ResponseContractError(
            "response root is not an object",
            category="invalid_contract",
        )
    if type(payload.get("st")) is not int or payload["st"] != 0:
        raise ResponseContractError(
            "response st is not zero",
            category="business_error",
        )
    # 响应 data 节点承载分页元数据和榜单数组。
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ResponseContractError("response data is missing", category="invalid_contract")
    # 真实榜单数组位于 data.data_result。
    data_result = data.get("data_result")
    if not isinstance(data_result, list):
        raise ResponseContractError(
            "data_result is not an array",
            category="invalid_contract",
        )
    # 真实分页元数据位于 data.page_result。
    page_result = data.get("page_result")
    if not isinstance(page_result, dict):
        raise ResponseContractError(
            "page_result is missing",
            category="invalid_contract",
        )
    # 响应页码必须和当前请求一致。
    response_page_no = page_result.get("page_no")
    if type(response_page_no) is not int or response_page_no != requested_page:
        raise ResponseContractError(
            "response page_no does not match request",
            category="page_mismatch",
        )
    # 响应每页条数必须保持已验证的固定值。
    response_page_size = page_result.get("page_size")
    if type(response_page_size) is not int or response_page_size != PAGE_SIZE:
        raise ResponseContractError(
            "response page_size is not ten",
            category="page_size_mismatch",
        )
    # 平台总数允许为 0，但 bool、负数和其他类型都违反契约。
    total = page_result.get("total")
    if type(total) is not int or total < 0:
        raise ResponseContractError(
            "response total is not a non-negative integer",
            category="invalid_total",
        )
    if expected_total is not None and total != expected_total:
        raise ResponseContractError(
            "response total changed during pagination",
            category="total_changed",
        )
    # 页数计划不设置 200 条或其他上限，完全覆盖接口 total。
    pagination_plan = calculate_pagination_plan(total)
    if requested_page > pagination_plan.target_page_count:
        raise ResponseContractError(
            "requested page is beyond total",
            category="page_out_of_range",
        )
    # 空榜单首页必须为空；正数榜单中间页满 10 条，末页允许 1～10 条。
    if total == 0:
        expected_item_count = 0
    else:
        remaining_items = total - ((requested_page - 1) * PAGE_SIZE)
        expected_item_count = min(PAGE_SIZE, remaining_items)
    if len(data_result) != expected_item_count:
        raise ResponseContractError(
            "response item count does not match pagination contract",
            category="item_count_mismatch",
        )
    return PageContract(
        api_total=total,
        target_page_count=pagination_plan.target_page_count,
        item_count=len(data_result),
    )


def parse_metric_range(
    metric_payload: Any,
    *,
    expected_unit: str,
    field_name: str,
) -> MetricRange:
    """Parse one verified two-value metric range without scaling raw values."""

    if not isinstance(metric_payload, dict):
        raise ResponseContractError(
            f"{field_name} is not an object",
            category="invalid_product",
        )
    # 平台区间原值位于 value_range 的两个元素中。
    value_range = metric_payload.get("value_range")
    if not isinstance(value_range, list) or len(value_range) != 2:
        raise ResponseContractError(
            f"{field_name}.value_range must contain two items",
            category="invalid_product",
        )
    # 区间下界和上界使用同一个严格字段校验。
    parsed_values: list[int] = []
    for range_item in value_range:
        if not isinstance(range_item, dict):
            raise ResponseContractError(
                f"{field_name} range item is not an object",
                category="invalid_product",
            )
        # 数值必须是非负整数，布尔值不能冒充整数。
        raw_value = range_item.get("value")
        if type(raw_value) is not int or raw_value < 0:
            raise ResponseContractError(
                f"{field_name} range value is invalid",
                category="invalid_product",
            )
        if range_item.get("unit") != expected_unit:
            raise ResponseContractError(
                f"{field_name} range unit changed",
                category="invalid_product",
            )
        parsed_values.append(raw_value)
    if parsed_values[0] > parsed_values[1]:
        raise ResponseContractError(
            f"{field_name} range is reversed",
            category="invalid_product",
        )
    return MetricRange(
        min_value=parsed_values[0],
        max_value=parsed_values[1],
        unit=expected_unit,
    )


def parse_page_entries(
    payload: dict[str, Any],
    *,
    page_no: int,
    captured_at: datetime,
) -> list[ProductRankEntry]:
    """Parse all product rows from one already validated page response."""

    # 领域记录最终受 SQLite page_no >= 1 约束，解析入口提前保持一致。
    if type(page_no) is not int or page_no < 1:
        raise ValueError("page_no must be a positive integer")
    # 页级契约已确认该路径为数组。
    data_result = payload["data"]["data_result"]
    # 解析结果在全部页完成后进入整榜校验。
    parsed_entries: list[ProductRankEntry] = []
    for item in data_result:
        if not isinstance(item, dict):
            raise ResponseContractError(
                "product row is not an object",
                category="invalid_product",
            )
        # 商品基本信息位于 product_info。
        product_info = item.get("product_info")
        if not isinstance(product_info, dict):
            raise ResponseContractError(
                "product_info is missing",
                category="invalid_product",
            )
        # 商品 ID 是整榜去重和持久化的稳定标识。
        product_id = product_info.get("id")
        if not isinstance(product_id, str) or not product_id:
            raise ResponseContractError(
                "product id is invalid",
                category="invalid_product",
            )
        # 商品名称必须是非空字符串才能导出 CSV。
        product_name = product_info.get("name")
        if not isinstance(product_name, str) or not product_name:
            raise ResponseContractError(
                "product name is invalid",
                category="invalid_product",
            )
        # 图片地址属于可选展示字段，缺失时不影响榜单采集和历史数据兼容。
        image_url = product_info.get("image_url")
        if image_url is not None and (
            not isinstance(image_url, str) or not image_url.strip()
        ):
            raise ResponseContractError(
                "product image url is invalid",
                category="invalid_product",
            )
        if isinstance(image_url, str):
            image_url = image_url.strip()
        # 排名必须是正整数，整榜连续性在后续统一校验。
        rank = product_info.get("rank")
        if type(rank) is not int or rank <= 0:
            raise ResponseContractError(
                "product rank is invalid",
                category="invalid_product",
            )
        # 首次上榜标记必须保持平台原始布尔语义。
        newly_on_ranking = product_info.get("newly_on_ranking")
        if type(newly_on_ranking) is not bool:
            raise ResponseContractError(
                "newly_on_ranking is not boolean",
                category="invalid_product",
            )
        # 店铺数组允许为空，但每个已返回店铺必须完整。
        shop_list = product_info.get("shop_list")
        if not isinstance(shop_list, list):
            raise ResponseContractError(
                "shop_list is not an array",
                category="invalid_product",
            )
        # 店铺关系按接口原始顺序从 0 编号。
        shops: list[ProductShop] = []
        for position, shop_payload in enumerate(shop_list):
            if not isinstance(shop_payload, dict):
                raise ResponseContractError(
                    "shop item is not an object",
                    category="invalid_product",
                )
            # 店铺 ID 保持平台原始字符串语义。
            shop_id = shop_payload.get("shop_id")
            # 店铺名称用于 CSV 顺序拼接。
            shop_name = shop_payload.get("shop_name")
            if not isinstance(shop_id, str) or not shop_id:
                raise ResponseContractError(
                    "shop id is invalid",
                    category="invalid_product",
                )
            if not isinstance(shop_name, str) or not shop_name:
                raise ResponseContractError(
                    "shop name is invalid",
                    category="invalid_product",
                )
            shops.append(
                ProductShop(position=position, shop_id=shop_id, shop_name=shop_name)
            )
        # 金额和成交件数分别按已验证单位保存原值。
        pay_amount = parse_metric_range(
            item.get("new_pay_amt"),
            expected_unit="price",
            field_name="new_pay_amt",
        )
        pay_combo_count = parse_metric_range(
            item.get("pay_combo_cnt"),
            expected_unit="number",
            field_name="pay_combo_cnt",
        )
        parsed_entries.append(
            ProductRankEntry(
                page_no=page_no,
                captured_at=captured_at,
                rank=rank,
                product_id=product_id,
                product_name=product_name,
                newly_on_ranking=newly_on_ranking,
                pay_amount=pay_amount,
                pay_combo_count=pay_combo_count,
                shops=tuple(shops),
                image_url=image_url,
            )
        )
    return parsed_entries


def validate_complete_ranking(
    entries: list[ProductRankEntry],
    *,
    api_total: int,
) -> None:
    """Reject incomplete, duplicate, or discontinuous full ranking snapshots."""

    # 完整榜单总数使用与分页计划相同的严格非负整数语义。
    if type(api_total) is not int or api_total < 0:
        raise ValueError("api_total must be a non-negative integer")
    if len(entries) != api_total:
        raise ResponseContractError(
            "full ranking item count does not match target",
            category="incomplete_ranking",
        )
    # 商品 ID 集合用于拒绝分页之间的重复商品。
    product_ids = {entry.product_id for entry in entries}
    if len(product_ids) != api_total:
        raise ResponseContractError(
            "full ranking contains duplicate products",
            category="duplicate_product",
        )
    # 重复排名单独分类，便于区分重复与单纯缺失或越界。
    ranks = {entry.rank for entry in entries}
    if len(ranks) != api_total:
        raise ResponseContractError(
            "full ranking contains duplicate ranks",
            category="duplicate_rank",
        )
    # 排名集合必须精确覆盖 1 到接口完整总数。
    expected_ranks = set(range(1, api_total + 1))
    if ranks != expected_ranks:
        raise ResponseContractError(
            "full ranking is not continuous",
            category="invalid_ranking_sequence",
        )
