"""Synthetic live response tests; these do not claim real account acceptance."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from compass_collector.errors import ResponseContractError
from compass_collector.platforms.taobao_categories import parse_category_tree
from compass_collector.platforms.taobao_config import (
    TaobaoCategoryScopeConfig, TaobaoDateConfig, TaobaoFiltersConfig, TaobaoRankConfig,
)
from compass_collector.platforms.taobao_product_rank import (
    RANK_PATH, build_expected_params, matches_rank_request, normalize_url,
    parse_metric_value, parse_page_entries, validate_complete_ranking, validate_page_payload,
)
from test_taobao_categories import TREE

# 固定时间只服务于合成测试，生产任务按实际北京时间生成。
CAPTURED_AT = datetime(2026, 10, 1, 14, tzinfo=ZoneInfo("Asia/Shanghai"))


def page_payload(page=1, total=41, update_time="2026-10-01 14:01:00"):
    """Generate the observed nested envelope with deterministic synthetic IDs."""
    # 偏移和条数让测试实际覆盖二十条及末页余数。
    start = (page - 1) * 20
    size = max(0, min(20, total - start))
    rows = [
        {
            "itemId": {"value": str(100000 + rank)},
            "item": {"itemId": str(100000 + rank), "title": f"合成商品{rank}",
                     "pictUrl": "//img.alicdn.com/synthetic.jpg",
                     "detailUrl": f"//sycm.taobao.com/mc/common/tm_item_redirect.htm?mi_id=synthetic-{rank}"},
            "shop": {"title": "合成店铺", "shopUrl": "//synthetic.tmall.com", "userId": 123},
            "payByrCnt": {"value": "2.5万 ~ 5万"}, "uv": {"value": None},
            "cateRankId": {"value": rank},
        }
        for rank in range(start + 1, start + size + 1)
    ]
    return {"code": 0, "data": {"updateTime": update_time, "data": {"recordCount": total, "data": rows}}}


def test_three_pages_preserve_raw_values_links_and_null():
    """Twenty, twenty, one validates exact count and seller identity semantics."""
    # 不同页更新时间故意不同，完整性校验不能以此拒绝实时榜。
    entries = []
    for page in range(1, 4):
        payload = page_payload(page, update_time=f"2026-10-01 14:0{page}:00")
        contract = validate_page_payload(payload, requested_page=page, expected_total=41)
        assert contract.target_page_count == 3
        entries.extend(parse_page_entries(payload, page_no=page, captured_at=CAPTURED_AT))
    validate_complete_ranking(entries, api_total=41)
    assert entries[0].pay_buyer_count_raw == "2.5万 ~ 5万"
    assert entries[0].pay_buyer_count.min_value == Decimal(25000)
    assert entries[0].visitor_count is None and entries[0].visitor_count_raw is None
    assert entries[0].newly_on_ranking is None and entries[0].pay_amount is None
    assert entries[0].product_url.startswith("https://sycm.taobao.com/")
    assert entries[0].shops[0].shop_id is None
    assert entries[0].shops[0].seller_user_id == "123"


def test_missing_shop_title_preserves_product_and_seller():
    """真实彩漂响应仅返回卖家标识时，不能丢弃完整商品及其排名。"""
    # 重现真实响应中缺少 title 和 shopUrl 的店铺对象。
    payload = page_payload(total=1)
    payload["data"]["data"]["data"][0]["shop"] = {"b2CShop": False, "userId": 123}
    # 使用正式解析和整榜校验，确保缺失店名不会导致整类失败。
    entries = parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT)
    validate_complete_ranking(entries, api_total=1)
    assert entries[0].product_id == "100001"
    assert entries[0].shops[0].shop_name == "未知"
    assert entries[0].shops[0].seller_user_id == "123"
    assert entries[0].shops[0].shop_url is None


@pytest.mark.parametrize("title", [None, "", " "])
def test_empty_shop_title_is_unknown(title):
    """null 和空文本按缺失处理，保留整个商品页。"""
    # 用户明确要求所有缺失店名统一保存为未知。
    payload = page_payload(total=1)
    payload["data"]["data"]["data"][0]["shop"]["title"] = title
    assert parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT)[0].shops[0].shop_name == "未知"


@pytest.mark.parametrize("title", [123, False, {}])
def test_malformed_present_shop_title_is_rejected(title):
    """错误类型仍然表示契约异常，不能伪装成缺失店名。"""
    # 每个用例仅替换明确存在的店名字段。
    payload = page_payload(total=1)
    payload["data"]["data"]["data"][0]["shop"]["title"] = title
    with pytest.raises(ResponseContractError):
        parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT)


@pytest.mark.parametrize("raw,minimum,maximum", [
    ("3", 3, 3), ("0 ~ 10", 0, 10), ("2.5万 ~ 5万", 25000, 50000),
    ("1亿", 100000000, 100000000), ("1.2万", 12000, 12000),
])
def test_metric_source_text_and_actual_units(raw, minimum, maximum):
    """Do not reformat raw counts or confuse Chinese magnitude with base units."""
    # 原文和规范化范围必须同时保留。
    original, metric = parse_metric_value(raw)
    assert original == raw
    assert (metric.min_value, metric.max_value, metric.unit) == (minimum, maximum, "count")


@pytest.mark.parametrize("raw", [None, "-"])
def test_missing_metric_has_no_numeric_zero(raw):
    """Missing values remain unknown, distinct from the actual value zero."""
    assert parse_metric_value(raw) == (raw, None)
    assert parse_metric_value("0")[1].min_value == 0


@pytest.mark.parametrize("raw", ["", "5 ~ 3", "-1", "3 ~ 5 ~ 7", "NaN", "1e4", "1.5", True, 10, "3万junk"])
def test_malformed_metric_is_rejected(raw):
    """Reject garbage instead of presenting a invented zero or interval."""
    with pytest.raises(ResponseContractError):
        parse_metric_value(raw)


def test_request_match_rejects_old_mode_date_size_and_duplicate_params():
    """A valid token is unnecessary; only the complete business query matches."""
    # 测试任务使用真正的淘宝业务类型，但不创建浏览器。
    task = SimpleNamespace(rank=TaobaoRankConfig(), filters=TaobaoFiltersConfig(), date=TaobaoDateConfig())
    scope = parse_category_tree(TREE, TaobaoCategoryScopeConfig()).categories[0]
    params = build_expected_params(task, scope, date(2026, 10, 1), 2)
    url = "https://sycm.taobao.com" + RANK_PATH + "?" + urlencode(params)
    assert matches_rank_request(SimpleNamespace(url=url), params)
    assert not matches_rank_request(SimpleNamespace(url=url.replace("/live/", "/offline/")), params)
    assert not matches_rank_request(SimpleNamespace(url=url + "&page=1"), params)
    assert not matches_rank_request(SimpleNamespace(url=url.replace("pageSize=20", "pageSize=10")), params)
    assert not matches_rank_request(SimpleNamespace(url=url.replace("2026-10-01", "2026-09-30")), params)
    assert not matches_rank_request(SimpleNamespace(url=url.replace("sycm.taobao.com", "evil.example")), params)
    assert not any(key in params for key in ("token", "_", "cookie"))


@pytest.mark.parametrize("mutation", ["total", "short", "extra", "bool_code", "bool_total", "wrong_ids", "wrong_rank", "missing_metric"])
def test_bad_page_cannot_be_published(mutation):
    """Catch malformed envelopes, pages, identities and source fields."""
    # 每个用例只改变一个已经成功的页面条件。
    payload = page_payload()
    data = payload["data"]["data"]
    if mutation == "total":
        data["recordCount"] = 42
    elif mutation == "short":
        data["data"].pop()
    elif mutation == "extra":
        data["data"].append(deepcopy(data["data"][0]))
    elif mutation == "bool_code":
        payload["code"] = False
    elif mutation == "bool_total":
        data["recordCount"] = True
    elif mutation == "wrong_ids":
        data["data"][0]["itemId"]["value"] = "different"
    elif mutation == "wrong_rank":
        data["data"][0]["cateRankId"]["value"] = 21
    else:
        del data["data"][0]["uv"]
    with pytest.raises(ResponseContractError):
        validate_page_payload(payload, requested_page=1, expected_total=41)
        parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT)


def test_empty_page_and_repeated_product_ids_preserve_ranking_positions():
    """Repeated Taobao IDs are valid while every source rank is retained."""
    assert validate_page_payload(page_payload(total=0), requested_page=1).target_page_count == 1
    with pytest.raises(ResponseContractError):
        validate_page_payload(page_payload(total=0), requested_page=2)
    # 独立完整性测试通过替换身份创建跨页重复，而不是只构造正常对象。
    entries = parse_page_entries(page_payload(total=2), page_no=1, captured_at=CAPTURED_AT)
    entries[1] = replace(entries[1], product_id=entries[0].product_id)
    validate_complete_ranking(entries, api_total=2)
    assert [entry.rank for entry in entries] == [1, 2]
    # 同商品允许重复，但同排名重复仍然表示不完整的榜单。
    entries[1] = replace(entries[1], rank=1)
    with pytest.raises(ResponseContractError) as error:
        validate_complete_ranking(entries, api_total=2)
    assert error.value.category == "duplicate_rank"


def test_observed_empty_list_envelope_is_a_successful_zero_row_category():
    """真实砧板喷雾等空榜返回 data.data=[]，不带 recordCount。"""
    # 固定业务正文重现真实成功响应，保留外层更新时间信息。
    payload = {"code": 0, "message": "操作成功", "data": {
        "updateTime": "2026-10-04 13:19:28", "timestamp": 1791091168946, "data": [],
    }}
    assert validate_page_payload(payload, requested_page=1).api_total == 0
    assert validate_page_payload(payload, requested_page=1).target_page_count == 1
    assert parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT) == []
    validate_complete_ranking([], api_total=0)
    with pytest.raises(ResponseContractError):
        validate_page_payload(payload, requested_page=2)
    with pytest.raises(ResponseContractError):
        validate_page_payload(payload, requested_page=1, expected_total=20)


@pytest.mark.parametrize("data", [None, {}, [None], [{"item": {}}]])
def test_other_incomplete_envelopes_are_not_empty_successes(data):
    """仅兼容已确认的空列表，其他不完整容器继续失败。"""
    with pytest.raises(ResponseContractError):
        validate_page_payload({"code": 0, "data": {"data": data}}, requested_page=1)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "https://user:password@sycm.taobao.com/item", "https://[broken", "https://example.test:bad/item"])
def test_unsafe_links_are_not_exported(url):
    """Only normal credential-free HTTP(S) links enter public records."""
    with pytest.raises(ResponseContractError):
        normalize_url(url, required=True)


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5])
def test_invalid_page_number_is_rejected_before_parsing(invalid):
    """Caller mistakes must not produce shifted or boolean page identities."""
    with pytest.raises(ValueError):
        parse_page_entries(page_payload(total=1), page_no=invalid, captured_at=CAPTURED_AT)


@pytest.mark.parametrize("invalid", [True, -1, 1.5])
def test_invalid_expected_total_is_not_treated_as_a_stable_count(invalid):
    """Reject caller totals that are not genuine non-negative integers."""
    with pytest.raises(ValueError):
        validate_page_payload(page_payload(total=1), requested_page=1, expected_total=invalid)
