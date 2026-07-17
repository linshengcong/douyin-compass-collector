"""Pure uncapped pagination-plan tests for one dynamic category."""

import pytest

from compass_collector.product_rank import calculate_pagination_plan


@pytest.mark.parametrize(
    ("total", "expected_page_count"),
    [
        (0, 1),
        (1, 1),
        (10, 1),
        (11, 2),
        (73, 8),
        (200, 20),
        (201, 21),
        (500, 50),
    ],
)
def test_pagination_covers_the_complete_api_total(
    total: int,
    expected_page_count: int,
) -> None:
    """Request every ten-item page without a two-hundred-item cap."""

    # 分页计划保留接口完整 total，不再计算任何裁剪后的目标条数。
    plan = calculate_pagination_plan(total)

    assert plan.api_total == total
    assert plan.target_page_count == expected_page_count


@pytest.mark.parametrize("invalid_total", [-1, True, False, 1.5, "10", None])
def test_pagination_rejects_negative_boolean_and_non_integer_totals(
    invalid_total: object,
) -> None:
    """Keep zero valid while rejecting values that only resemble totals."""

    # bool 是 int 的子类，必须由实现显式拒绝。
    with pytest.raises(ValueError, match="non-negative integer"):
        calculate_pagination_plan(invalid_total)  # type: ignore[arg-type]
