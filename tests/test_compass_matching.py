"""Prevent intermediate, stale and ambiguous responses entering the UI PoC."""

from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from compass_collector.platforms.compass import RANK_PATH, matches_rank_response

# 样本只包含业务字段，认证参数使用无效占位字符串。
EXPECTED = {
    "industry_id": "5",
    "date_type": 1,
    "category_id": "1000004647,1000004649",
    "page_no": 2,
    "page_size": 10,
    "brand_type": 0,
    "price_bin": "不限",
    "begin_date": "2026/09/30 00:00:00",
    "search_info": "",
}


def test_accepts_exact_business_fields_with_extra_signature() -> None:
    """Page-generated signature fields do not alter the business match."""
    # 无效签名占位仅证明匹配逻辑不依赖认证值。
    response = SimpleNamespace(
        url=f"https://compass.jinritemai.com{RANK_PATH}?{urlencode(EXPECTED)}&signature=invalid-placeholder"
    )
    assert matches_rank_response(response, EXPECTED)


@pytest.mark.parametrize(
    "field,value",
    [
        ("industry_id", "8"),
        ("date_type", 2),
        ("category_id", "1000003462"),
        ("page_no", 1),
        ("brand_type", -1),
        ("price_bin", "0-9"),
        ("begin_date", "2026/09/29 00:00:00"),
        ("search_info", "other"),
    ],
)
def test_rejects_intermediate_or_stale_response(field, value) -> None:
    """Different categories, pages, filters or dates must never be accepted."""
    # 只修改一个业务字段，覆盖分类切换和旧请求晚到的情形。
    query = dict(EXPECTED, **{field: value})
    # 假响应不执行任何网络请求。
    response = SimpleNamespace(
        url=f"https://compass.jinritemai.com{RANK_PATH}?{urlencode(query)}"
    )
    assert not matches_rank_response(response, EXPECTED)


def test_rejects_missing_blank_parameter_and_duplicate_page() -> None:
    """Missing blank fields and duplicate query parameters are ambiguous."""
    # 缺失空搜索字段不能等同于已确认的空搜索条件。
    query = {key: value for key, value in EXPECTED.items() if key != "search_info"}
    # 重复页码不能选择其中一个值冒充唯一响应。
    urls = [
        f"https://compass.jinritemai.com{RANK_PATH}?{urlencode(query)}",
        f"https://compass.jinritemai.com{RANK_PATH}?{urlencode(EXPECTED)}&page_no=2",
        f"https://example.com{RANK_PATH}?{urlencode(EXPECTED)}",
    ]
    for url in urls:
        assert not matches_rank_response(SimpleNamespace(url=url), EXPECTED)
