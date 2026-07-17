"""Product-ranking request and page contracts using one real-shaped fixture."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from compass_collector.errors import ResponseContractError
from compass_collector.models import DiscoveredCategory
from compass_collector.product_rank import build_request_params, validate_page_payload
from current_contract import CURRENT_BRAND_TYPE, CURRENT_PRICE_BIN, CURRENT_TASK


# 测试样本是用户提供真实响应的脱敏裁剪副本。
FIXTURE_PATH = Path("tests/fixtures/product_rank_page.json")
# 动态、追踪和签名参数永远不能进入业务请求。
FORBIDDEN_DYNAMIC_PARAMS = {"_lid", "verifyFp", "fp", "msToken", "a_bogus"}


def load_page_fixture() -> dict[str, Any]:
    """Load a fresh product-ranking page for isolated contract mutations."""

    # 每次重新解析 JSON，避免测试之间共享 data_result 或 page_result。
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def build_discovered_category() -> DiscoveredCategory:
    """Build one dynamic level-three category for request-parameter tests."""

    # 真实级联 ID 用于锁定二级与三级分类共同进入 category_id。
    return DiscoveredCategory(
        discovery_order=1,
        level1_category_id="13",
        level1_category_name="食品饮料",
        level2_category_id="1000001823",
        level2_category_name="水饮冲调",
        category_id="1000001832",
        category_name="冲饮谷物",
    )


def assert_page_error(
    payload: dict[str, Any],
    *,
    requested_page: int,
    expected_total: int | None,
    expected_category: str,
) -> None:
    """Assert one malformed page fails with a stable safe category."""

    # 错误分类会进入 CategoryRun、Manifest 和最终批次汇总。
    with pytest.raises(ResponseContractError) as error_info:
        validate_page_payload(
            payload,
            requested_page=requested_page,
            expected_total=expected_total,
        )
    assert error_info.value.category == expected_category


def test_real_fixture_matches_verified_first_page_contract() -> None:
    """Recognize the real first page without treating it as a full ranking."""

    # Fixture 明确表示 total=200 的第一页 10 条，而不是完整十条榜单。
    payload = load_page_fixture()
    # 页级契约同时暴露完整 total、目标页数和当前页条数。
    contract = validate_page_payload(payload, requested_page=1, expected_total=None)

    assert contract.api_total == 200
    assert contract.target_page_count == 20
    assert contract.item_count == 10


def test_request_params_use_cascaded_category_and_exact_twelve_fields() -> None:
    """Build the verified fields with the full level-two/level-three category path."""

    # 真实任务配置只提供榜单、筛选和日期策略。
    task = CURRENT_TASK
    # 当次分类树发现结果提供动态行业和二级、三级级联 ID。
    category = build_discovered_category()
    # 第八页用于证明页码不会被固定在第一页。
    params = build_request_params(task, category, date(2026, 7, 16), page_no=8)

    assert params == {
        "page_no": 8,
        "page_size": 10,
        "industry_id": "13",
        "category_id": "1000001823,1000001832",
        "brand_type": CURRENT_BRAND_TYPE,
        "price_bin": CURRENT_PRICE_BIN,
        "search_info": "",
        "rank_data_type": 1,
        "begin_date": "2026/07/16 00:00:00",
        "end_date": "2026/07/16 00:00:00",
        "date_type": 1,
        "activity_id": "",
    }
    assert len(params) == 12
    assert FORBIDDEN_DYNAMIC_PARAMS.isdisjoint(params)


def test_empty_ranking_is_one_successful_zero_item_page() -> None:
    """Accept total zero only on an empty first page."""

    # 空榜单仍保留真实第一页响应及固定 page_size。
    payload = load_page_fixture()
    payload["data"]["page_result"]["total"] = 0
    payload["data"]["data_result"] = []
    # 空榜单分页计划必须为一页而不是零页。
    contract = validate_page_payload(payload, requested_page=1, expected_total=None)

    assert contract.api_total == 0
    assert contract.target_page_count == 1
    assert contract.item_count == 0


@pytest.mark.parametrize(
    ("total", "page_no", "expected_items", "expected_pages"),
    [
        (73, 7, 10, 8),
        (73, 8, 3, 8),
        (20, 2, 10, 2),
        (201, 21, 1, 21),
    ],
)
def test_intermediate_and_final_pages_require_exact_item_counts(
    total: int,
    page_no: int,
    expected_items: int,
    expected_pages: int,
) -> None:
    """Handle partial and exact-multiple final pages without a 200-row cap."""

    # Fixture 行只提供真实字段形状，页码、total 和行数按边界场景裁剪。
    payload = load_page_fixture()
    payload["data"]["page_result"].update({"page_no": page_no, "total": total})
    payload["data"]["data_result"] = payload["data"]["data_result"][:expected_items]
    # 后续页必须继续匹配第一页已经固定的 total。
    contract = validate_page_payload(
        payload,
        requested_page=page_no,
        expected_total=total,
    )

    assert contract.api_total == total
    assert contract.target_page_count == expected_pages
    assert contract.item_count == expected_items


def test_total_change_is_rejected_without_replanning() -> None:
    """Fail a live category when a later page changes the first-page total."""

    # 后续响应 total 从 200 变化到 201 时不能重新计算并继续拼接。
    payload = load_page_fixture()
    payload["data"]["page_result"]["total"] = 201

    assert_page_error(
        payload,
        requested_page=1,
        expected_total=200,
        expected_category="total_changed",
    )


@pytest.mark.parametrize("invalid_total", [-1, True, False, 1.5, "0", None])
def test_response_total_rejects_negative_boolean_and_non_integer_values(
    invalid_total: object,
) -> None:
    """Accept integer zero while rejecting values that only resemble totals."""

    # 每个场景只替换真实 page_result.total 字段。
    payload = load_page_fixture()
    payload["data"]["page_result"]["total"] = invalid_total

    assert_page_error(
        payload,
        requested_page=1,
        expected_total=None,
        expected_category="invalid_total",
    )


def test_page_beyond_empty_ranking_is_rejected() -> None:
    """Prevent a second request after a zero-total first page."""

    # total=0 的唯一合法响应页是 page 1。
    payload = load_page_fixture()
    payload["data"]["page_result"].update({"page_no": 2, "total": 0})
    payload["data"]["data_result"] = []

    assert_page_error(
        payload,
        requested_page=2,
        expected_total=0,
        expected_category="page_out_of_range",
    )


@pytest.mark.parametrize("invalid_item_count", [2, 4])
def test_partial_final_page_rejects_too_few_or_too_many_rows(
    invalid_item_count: int,
) -> None:
    """Require exactly three rows on page eight when total is seventy-three."""

    # 末页分别保留两行或四行，覆盖少一条和多一条。
    payload = load_page_fixture()
    payload["data"]["page_result"].update({"page_no": 8, "total": 73})
    payload["data"]["data_result"] = payload["data"]["data_result"][
        :invalid_item_count
    ]

    assert_page_error(
        payload,
        requested_page=8,
        expected_total=73,
        expected_category="item_count_mismatch",
    )


@pytest.mark.parametrize(
    ("metadata_case", "expected_category"),
    [
        ("page_no", "page_mismatch"),
        ("page_size", "page_size_mismatch"),
        ("data_result", "invalid_contract"),
        ("page_result", "invalid_contract"),
    ],
)
def test_page_metadata_contract_errors_are_classified(
    metadata_case: str,
    expected_category: str,
) -> None:
    """Reject page identity, size and structural response drift."""

    # 每个 case 只破坏真实 Fixture 中的一处页级字段。
    payload = load_page_fixture()
    if metadata_case == "page_no":
        # 响应页码必须与请求页码完全一致。
        payload["data"]["page_result"]["page_no"] = 2
    elif metadata_case == "page_size":
        # 接口固定 page_size=10，不能随 total 改变。
        payload["data"]["page_result"]["page_size"] = 20
    elif metadata_case == "data_result":
        # 榜单行集合必须保持数组结构。
        payload["data"]["data_result"] = {}
    else:
        # 分页元数据必须保持对象结构。
        payload["data"]["page_result"] = []

    assert_page_error(
        payload,
        requested_page=1,
        expected_total=None,
        expected_category=expected_category,
    )


@pytest.mark.parametrize("invalid_page_no", [0, -1, True, False])
def test_request_params_reject_invalid_page_numbers(invalid_page_no: object) -> None:
    """Reject zero, negative and boolean request page numbers."""

    # 动态分类和任务配置保持有效，只测试页码入口。
    task = CURRENT_TASK
    category = build_discovered_category()

    with pytest.raises(ValueError, match="positive integer"):
        build_request_params(
            task,
            category,
            date(2026, 7, 16),
            page_no=invalid_page_no,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("invalid_expected_total", [-1, True, False, 1.5])
def test_page_contract_rejects_invalid_expected_totals(
    invalid_expected_total: object,
) -> None:
    """Reject invalid first-page totals passed back into later-page validation."""

    # 真实响应保持有效，失败只来自调用方保存的 expected_total。
    payload = load_page_fixture()

    with pytest.raises(ValueError, match="non-negative integer"):
        validate_page_payload(
            payload,
            requested_page=1,
            expected_total=invalid_expected_total,  # type: ignore[arg-type]
        )
