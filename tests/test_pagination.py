"""Pure pagination-plan tests."""

from compass_collector.product_rank import calculate_pagination_plan


def test_total_below_cap_uses_partial_final_page() -> None:
    """Request sixteen pages when the platform total is 156."""

    # 总数小于任务上限时完整覆盖平台榜单。
    plan = calculate_pagination_plan(total=156, max_items=200)
    assert plan.target_items == 156
    assert plan.target_pages == 16


def test_total_above_cap_stops_at_two_hundred_items() -> None:
    """Request twenty pages when the platform total exceeds the cap."""

    # 总数大于任务上限时仅采集前 200 条。
    plan = calculate_pagination_plan(total=500, max_items=200)
    assert plan.target_items == 200
    assert plan.target_pages == 20
