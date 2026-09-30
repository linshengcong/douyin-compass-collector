"""Platform boundaries, configuration, response ownership and recovery policies."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import pytest
import yaml
from pydantic import ValidationError
from compass_collector.config import AppConfig, CategoryScopeConfig, load_config
from compass_collector.models import (
    CategoryDiscoveryResult,
    DiscoveredCategory,
    DiscoveredScope,
    MetricRange,
)
from compass_collector.errors import (
    AuthRequiredError,
    CollectionInterruptedError,
    ResponseContractError,
)
from compass_collector.exporter import format_metric_range
from compass_collector.platforms.compass import CompassAdapter, RANK_PATH, select_scopes
from compass_collector.run_control import CollectionControl

# Synthetic taxonomy keeps numeric business IDs and realistic ordered paths.
TREE = CategoryDiscoveryResult(
    None,
    None,
    tuple(
        DiscoveredCategory(
            i, "5", "个护家清", "1000004647", "家清纸品", str(1000004648 + i), name
        )
        for i, name in enumerate(["家庭环境清洁", "纸品"], 1)
    ),
)


def test_selection_order_and_full_discovery():
    """IDs resolve names from this tree while selected order stays authoritative."""
    # Reverse targets exercises configured order independently of discovery order.
    scope = CategoryScopeConfig(
        mode="selected",
        targets=[
            {"industry_id": "5", "category_id": "1000004647,1000004650"},
            {"industry_id": "5", "category_id": "1000004647,1000004649"},
        ],
    )
    selected = select_scopes(TREE, scope)
    assert [item.key for item in selected.categories] == ["1000004650", "1000004649"]
    assert [item.discovery_order for item in selected.categories] == [1, 2]
    assert selected.categories[1].path == ("个护家清", "家清纸品", "家庭环境清洁")
    assert [
        item.key for item in select_scopes(TREE, CategoryScopeConfig()).categories
    ] == ["1000004649", "1000004650"]


def test_industry_scope_excludes_other_industries_and_keeps_tree_order():
    """One industry collects every third-level sibling without crossing industries."""
    # 混合行业快照故意将其他行业插入中间，验证过滤与顺序独立。
    other = DiscoveredCategory(2, "8", "其他行业", "80", "其他二级", "800", "其他三级")
    # 原有两个三级节点属于同一行业，执行序号需要重新连续编号。
    mixed = CategoryDiscoveryResult(
        None, None, (TREE.categories[0], other, TREE.categories[1])
    )
    # 行业限定不能退化为仅采集一个指定三级节点。
    scope = CategoryScopeConfig(mode="all_level1", industry_id="5")
    selected = select_scopes(mixed, scope)
    assert [item.key for item in selected.categories] == ["1000004649", "1000004650"]
    assert [item.discovery_order for item in selected.categories] == [1, 2]
    assert all(
        item.platform_metadata["industry_id"] == "5" for item in selected.categories
    )
    assert len(select_scopes(mixed, CategoryScopeConfig()).categories) == 3


def test_missing_industry_fails_discovery_instead_of_expanding_scope():
    """An unavailable industry never falls back to all industries."""
    with pytest.raises(ResponseContractError) as error:
        select_scopes(TREE, CategoryScopeConfig(mode="all_level1", industry_id="8"))
    assert error.value.category == "invalid_configured_industry"


@pytest.mark.parametrize(
    "industry,category", [("8", "1000004647,1000004649"), ("5", "1000004647,999")]
)
def test_invalid_target_rejects_entire_selection(industry, category):
    """A valid sibling cannot mask an invalid industry or category pair."""
    scope = CategoryScopeConfig(
        mode="selected",
        targets=[
            {"industry_id": "5", "category_id": "1000004647,1000004650"},
            {"industry_id": industry, "category_id": category},
        ],
    )
    with pytest.raises(ResponseContractError, match="Invalid configured"):
        select_scopes(TREE, scope)


@pytest.mark.parametrize(
    "scope",
    [
        {"mode": "all_level1", "industry_id": "0"},
        {"mode": "all_level1", "industry_id": "5,8"},
        {
            "mode": "selected",
            "industry_id": "5",
            "targets": [{"industry_id": "5", "category_id": "1000004647,1000004649"}],
        },
        {"mode": "selected"},
        {
            "mode": "all_level1",
            "targets": [{"industry_id": "5", "category_id": "1000004647,1000004649"}],
        },
        {
            "mode": "selected",
            "targets": [{"industry_id": "5", "category_id": "1000004647,1000004649"}]
            * 2,
        },
    ],
)
def test_invalid_scope_configs_fail_before_browser(scope):
    """Reject ambiguous or contradictory task ranges before page execution."""
    with pytest.raises(ValidationError):
        CategoryScopeConfig.model_validate(scope)


@pytest.mark.parametrize("mutation", ["platform", "task", "primary"])
def test_unknown_platform_and_primary_fail_at_load(mutation):
    """Startup validation excludes unimplemented platforms and missing primary tasks."""
    config = yaml.safe_load(Path("config/tasks.yaml").read_text())
    if mutation == "platform":
        config["platforms"]["taobao"] = {"profile_dir": "runtime/taobao-profile"}
    elif mutation == "task":
        config["tasks"][0]["platform"] = "missing"
    else:
        config["publication"]["web_primary_task_id"] = "missing"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config)


def make_adapter(*, manual=True, control=None):
    """Construct an unopened adapter so policy tests cannot touch live profiles."""
    config = load_config(Path("config/tasks.yaml"))
    return CompassAdapter(
        config.browser_for("compass"),
        config.collection.model_copy(update={"manual_auth_wait_seconds": 0.01}),
        manual=manual,
        control=control,
    )


def test_late_old_request_identity_cannot_enter_pending():
    """Identical query strings do not authorize requests begun before the action."""
    adapter = make_adapter()
    expected = {"page_no": 2, "category_id": "1000004647,1000004649"}
    response = SimpleNamespace(
        url=f"https://compass.jinritemai.com{RANK_PATH}?{urlencode(expected)}"
    )

    # Request object identity, rather than URL equality, is the acceptance boundary.
    class Request:
        def __init__(self):
            """Expose only the network shape consumed by adapter listeners."""
            self.url = response.url

        def response(self):
            """Return the completed response fixture."""
            return response

    old = Request()
    adapter._on_request(old)
    adapter.expected = expected
    new = Request()
    adapter._on_request(new)
    response.request = old
    adapter._on_response(response)
    adapter._on_finished(old)
    assert adapter.pending == []
    response.request = new
    adapter._on_response(response)
    adapter._on_finished(new)
    assert adapter.pending == [response]


def test_scheduled_auth_never_waits_or_navigates():
    """An unattended authentication challenge immediately ends the task."""
    adapter = make_adapter(manual=False)
    with pytest.raises(AuthRequiredError):
        adapter._await_manual_auth(lambda: False)


@pytest.mark.parametrize(
    "manual,status,recovered_status,error_category",
    [
        (False, 10012, 0, "auth_required"),
        (False, 10008, 0, "auth_required"),
        (True, 10012, 0, None),
        (True, 10008, 0, None),
        (True, 10012, 10012, "auth_required"),
    ],
)
def test_discovery_auth_recovers_once_or_stops(
    monkeypatch, manual, status, recovered_status, error_category
):
    """Discovery shares authentication policy without unlimited reentry attempts."""
    # 时间由测试推进，无须真实 Chrome 或等待默认网络超时。
    clock = [0.0]
    monkeypatch.setattr(
        "compass_collector.platforms.compass.monotonic", lambda: clock[0]
    )
    # 未打开的适配器只能处理合成响应，不访问真实 Profile。
    adapter = make_adapter(manual=manual)
    adapter.open_session = lambda: None
    adapter.category_responses = [SimpleNamespace(json=lambda: {"st": status})]
    # 发现阶段选全部分类，合法 fixture 无需满足生产目标 ID。
    config = load_config(Path("config/tasks.yaml"))
    task = config.tasks[0].model_copy(update={"category_scope": CategoryScopeConfig()})
    # 恢复成功返回已有脱敏分类树，失败则重放平台认证错误。
    tree = json.loads(Path("tests/fixtures/category_tree.json").read_text())
    # 恢复次数与等待次数验证定时立即结束、人工恢复上限。
    calls = []

    def pump(seconds):
        """Advance past one response deadline without sleeping."""
        calls.append("wait")
        clock[0] += adapter.settings.response_timeout_seconds + 1

    def recover():
        """Require stale responses to be discarded before reentering the page."""
        assert adapter.category_responses == []
        calls.append("recover")
        adapter.category_responses.append(
            SimpleNamespace(
                json=lambda: tree if recovered_status == 0 else {"st": recovered_status}
            )
        )

    adapter._pump = pump
    adapter._recover_auth = recover
    if error_category:
        with pytest.raises(AuthRequiredError) as error:
            adapter.discover_scopes(task)
        assert error.value.category == error_category
    else:
        assert len(adapter.discover_scopes(task).discovery.categories) == 4
    assert calls.count("recover") == int(manual)
    if not manual:
        assert calls == []


def test_manual_auth_timeout_and_cancel_are_bounded():
    """Manual waiting expires and an already requested stop interrupts immediately."""
    adapter = make_adapter()
    adapter.page = SimpleNamespace(wait_for_timeout=lambda milliseconds: None)
    with pytest.raises(AuthRequiredError):
        adapter._await_manual_auth(lambda: False)
    control = CollectionControl()
    control.request_stop()
    cancelled = make_adapter(control=control)
    with pytest.raises(CollectionInterruptedError):
        cancelled._await_manual_auth(lambda: False)


def test_cross_day_stops_before_page_actions():
    """A frozen business date cannot continue collecting after local midnight."""
    # 未打开的适配器证明跨天检查发生在任何页面或网络动作前。
    adapter = make_adapter()
    # 配置提供一个有效目标，避免错误来自其他参数校验。
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    # 前一天日期稳定模拟本次采集已跨过北京时间午夜。
    prior_date = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    with pytest.raises(CollectionInterruptedError) as error:
        adapter._capture(task, TREE.categories[0], prior_date, 1)
    assert error.value.category == "business_date_changed"


def test_generic_scope_and_units_do_not_require_three_levels():
    """One-level platforms keep full paths and actual units in shared presentation."""
    scope = DiscoveredScope(1, "other-key", ("单层类目",), {"native_id": "other-key"})
    assert scope.display_path == "单层类目"
    assert scope.level1_category_id == ""
    assert scope.level2_category_id == ""
    assert format_metric_range(MetricRange(1000, 2500, "CNY")) == "¥1000-¥2500"
    assert format_metric_range(MetricRange(10, 25, "count")) == "10-25"


@pytest.mark.parametrize("initially_present", [True, False])
def test_pagination_scrolls_up_once_only_when_absent(initially_present):
    """Visible pagination causes no wheel calls; absent pagination gets one recovery."""
    adapter = make_adapter()
    calls = []

    class Pagination:
        """Appear immediately after the one permitted upward scroll."""

        present = initially_present

        def count(self):
            """Expose DOM existence independently from locator click behavior."""
            return int(self.present)

        def bounding_box(self, **kwargs):
            """Keep this fixture fully inside its stable viewport."""
            return {"x": 100, "y": 100, "width": 24, "height": 30}

        def evaluate(self, expression, **kwargs):
            """Expose a real hit target without triggering locator auto-scroll."""
            return {"x": 112, "y": 115}

        def is_visible(self):
            """Return the current observed display state."""
            return self.present

    pagination = Pagination()

    def wheel(x, y):
        """Record the scroll direction and emulate lazy pagination mounting."""
        calls.append(("wheel", x, y))
        pagination.present = True

    adapter.page = SimpleNamespace(
        mouse=SimpleNamespace(wheel=wheel, click=lambda x, y: calls.append(("click",))),
        locator=lambda selector: pagination,
        evaluate=lambda expression: 800,
    )

    def wait_after_scroll(seconds):
        """Verify the layout delay is inserted before clicking recovered pagination."""
        adapter._check_stopped()
        calls.append(("wait", seconds))

    adapter._pump = wait_after_scroll

    def forbidden_auto_scroll(locator):
        """Automatic locator clicks reproduce edge interception and must be avoided."""
        pytest.fail("Pagination must preserve its verified unobstructed click point")

    adapter._click = forbidden_auto_scroll
    adapter._next_page()
    assert calls == (
        [("click",)]
        if initially_present
        else [
            ("wheel", 0, -800),
            ("wait", adapter.settings.scroll_settle_seconds),
            ("click",),
        ]
    )
