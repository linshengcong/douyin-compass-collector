"""Real-response contract tests using the single sanitized fixture."""

import json
from datetime import date
from pathlib import Path

from compass_collector.config import load_config
from compass_collector.product_rank import build_request_params, validate_page_payload


# 测试样本是用户提供真实响应的脱敏裁剪副本。
FIXTURE_PATH = Path("tests/fixtures/product_rank_page.json")


def test_real_fixture_matches_verified_page_contract() -> None:
    """Recognize the real st, page_result, and data_result structure."""

    # 脱敏 Fixture 仅在本测试进程内加载。
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 契约结果仅暴露分页循环需要的非敏感摘要。
    contract = validate_page_payload(payload, requested_page=1, expected_total=None)
    assert contract.total == 200
    assert contract.item_count == 10


def test_request_params_contain_only_verified_business_fields() -> None:
    """Keep dynamic signatures and tracking parameters out of the HTTP request."""

    # 真实任务配置用于构造第一页的最小请求参数。
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    # 请求参数字典不应包含任何动态安全或追踪字段。
    params = build_request_params(task, date(2026, 7, 16), page_no=1)
    # 已排除参数名集合固定安全边界。
    excluded_names = {"_lid", "verifyFp", "fp", "msToken", "a_bogus"}

    assert excluded_names.isdisjoint(params)
    assert params["page_size"] == 10
    assert params["begin_date"] == "2026/07/16 00:00:00"
