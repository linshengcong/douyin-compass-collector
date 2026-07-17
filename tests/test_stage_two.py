"""Product-row parsing and complete category-ranking validation tests."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from compass_collector.errors import ResponseContractError
from compass_collector.exporter import format_metric_range
from compass_collector.product_rank import (
    parse_page_entries,
    validate_complete_ranking,
)


# 唯一真实形状 Fixture 表示 total=200 的第一页十条记录。
FIXTURE_PATH = Path("tests/fixtures/product_rank_page.json")
# 测试时间使用工程确认的北京时区。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def load_real_entries():
    """Parse ten page entries without claiming they form the full ranking."""

    # 脱敏真实响应在测试进程内加载。
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 固定捕获时间使解析结果可重复。
    captured_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    # Fixture 只用于验证单页商品字段，不在这里执行整榜校验。
    entries = parse_page_entries(payload, page_no=1, captured_at=captured_at)
    return payload, captured_at, entries


def assert_ranking_error(entries, *, api_total: int, expected_category: str) -> None:
    """Assert one invalid category snapshot reports a stable error category."""

    # 完整榜单错误分类会决定当前 CategoryRun 的失败日志。
    with pytest.raises(ResponseContractError) as error_info:
        validate_complete_ranking(entries, api_total=api_total)
    assert error_info.value.category == expected_category


def test_real_fixture_parses_and_formats_like_the_agreed_csv_contract() -> None:
    """Preserve raw values while formatting only the CSV presentation layer."""

    # 真实 Fixture 只解析为第一页十条，不伪装成 total=10 的完整榜单。
    _, captured_at, entries = load_real_entries()
    # 首名商品包含金额和成交件数的已验证区间。
    first_entry = entries[0]

    assert len(entries) == 10
    assert [entry.rank for entry in entries] == list(range(1, 11))
    assert all(entry.captured_at == captured_at for entry in entries)
    assert first_entry.pay_amount.min_value == 1_000_000_000
    assert first_entry.pay_combo_count.min_value == 100_000
    assert format_metric_range(first_entry.pay_amount) == "¥1000万-¥2500万"
    assert format_metric_range(first_entry.pay_combo_count) == "1万-2.5万"


def test_empty_category_ranking_is_complete_when_api_total_is_zero() -> None:
    """Treat a validated zero-total first page as a complete empty snapshot."""

    # 空榜单不生成任何商品领域记录。
    validate_complete_ranking([], api_total=0)


def test_complete_ranking_rejects_duplicate_product_ids() -> None:
    """Reject one product repeated at two ranks inside the same category."""

    # 两条记录保留不同 rank，但复用同一个 product_id。
    _, _, real_entries = load_real_entries()
    entries = [
        real_entries[0],
        replace(real_entries[1], product_id=real_entries[0].product_id),
    ]

    assert_ranking_error(
        entries,
        api_total=2,
        expected_category="duplicate_product",
    )


def test_complete_ranking_rejects_duplicate_ranks() -> None:
    """Reject two different products occupying the same category rank."""

    # 商品 ID 保持唯一，只把第二条 rank 改成 1。
    _, _, real_entries = load_real_entries()
    entries = [real_entries[0], replace(real_entries[1], rank=1)]

    assert_ranking_error(
        entries,
        api_total=2,
        expected_category="duplicate_rank",
    )


def test_complete_ranking_rejects_missing_or_out_of_range_rank() -> None:
    """Require the exact one-through-total rank set for one category."""

    # rank 3 让两条记录形成 {1, 3}，缺少 rank 2。
    _, _, real_entries = load_real_entries()
    entries = [real_entries[0], replace(real_entries[1], rank=3)]

    assert_ranking_error(
        entries,
        api_total=2,
        expected_category="invalid_ranking_sequence",
    )


def test_complete_ranking_rejects_incomplete_item_count() -> None:
    """Reject a category snapshot that stopped before the complete API total."""

    # 只提供一条但声明接口 total 为 2。
    _, _, real_entries = load_real_entries()

    assert_ranking_error(
        [real_entries[0]],
        api_total=2,
        expected_category="incomplete_ranking",
    )


@pytest.mark.parametrize("invalid_total", [-1, True, False, 1.5])
def test_complete_ranking_rejects_invalid_api_totals(invalid_total: object) -> None:
    """Reject negative, boolean and non-integer complete totals."""

    # 空 entries 确保失败只来自 api_total 类型契约。
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_complete_ranking(
            [],
            api_total=invalid_total,  # type: ignore[arg-type]
        )
