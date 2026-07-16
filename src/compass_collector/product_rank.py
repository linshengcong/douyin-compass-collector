"""Verified request and response contract for the product hot-sale ranking."""

from dataclasses import dataclass
from datetime import date
from math import ceil
from typing import Any

from compass_collector.config import TaskConfig
from compass_collector.errors import ResponseContractError


# 真实接口已验证每页固定返回最多 10 条。
PAGE_SIZE = 10


@dataclass(frozen=True, slots=True)
class PaginationPlan:
    """Describe how many complete API pages the current task must request."""

    target_items: int
    target_pages: int


@dataclass(frozen=True, slots=True)
class PageContract:
    """Expose the validated fields needed by the collection loop."""

    total: int
    item_count: int


def calculate_pagination_plan(total: int, max_items: int) -> PaginationPlan:
    """Calculate the capped item count and required ten-item pages."""

    if total <= 0:
        raise ValueError("total must be positive")
    # 目标条数取平台总数和任务上限中的较小值。
    target_items = min(total, max_items)
    # 页数向上取整，以覆盖平台总数不是 10 的倍数的情况。
    target_pages = ceil(target_items / PAGE_SIZE)
    return PaginationPlan(target_items=target_items, target_pages=target_pages)


def build_request_params(
    task: TaskConfig,
    business_date: date,
    page_no: int,
) -> dict[str, str | int]:
    """Build only the verified business query parameters."""

    # 平台日期参数在任务启动时固定，全部分页共用。
    platform_date = business_date.strftime("%Y/%m/%d 00:00:00")
    return {
        "page_no": page_no,
        "page_size": PAGE_SIZE,
        "industry_id": task.filters.industry.id,
        "category_id": task.filters.category.id,
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
    # 平台总数必须是正整数，空榜单按异常处理。
    total = page_result.get("total")
    if type(total) is not int or total <= 0:
        raise ResponseContractError(
            "response total is not positive",
            category="invalid_total",
        )
    if expected_total is not None and total != expected_total:
        raise ResponseContractError(
            "response total changed during pagination",
            category="total_changed",
        )
    # 当前页在整个平台榜单中的剩余条数用于校验末页。
    remaining_items = total - ((requested_page - 1) * PAGE_SIZE)
    if remaining_items <= 0:
        raise ResponseContractError(
            "requested page is beyond total",
            category="page_out_of_range",
        )
    # 中间页必须满 10 条，平台末页允许不足 10 条。
    expected_item_count = min(PAGE_SIZE, remaining_items)
    if len(data_result) != expected_item_count:
        raise ResponseContractError(
            "response item count does not match pagination contract",
            category="item_count_mismatch",
        )
    return PageContract(total=total, item_count=len(data_result))
