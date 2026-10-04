"""Exercise the actual registry and login lifecycle without a real account."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from compass_collector.config import CollectionConfig, load_config
from compass_collector.errors import BrowserOperationError, CollectionInterruptedError
from compass_collector.platforms.registry import create_adapter
from compass_collector.platforms.taobao_controls import TaobaoBrowserControls


def test_factory_accepts_poc_and_preserves_default_platform():
    """An explicit PoC config must not add a scheduled default task."""
    # 使用提交配置验证真正入口，避免只测试手工构造的 TaskConfig。
    config = load_config(Path("config/taobao-poc.yaml"))
    adapter = create_adapter("taobao", config.browser_for("taobao"), config.collection, manual=True)
    assert isinstance(adapter.controls, TaobaoBrowserControls)
    assert adapter.browser_config.profile_dir == Path("runtime/taobao-browser-profile")
    assert adapter.browser_config.webdriver_compatibility is True
    assert adapter.browser_config.persist_session_cookies is True
    # 手动PoC必须保留最终页面，防止错误清理表现为打开后立即关闭。
    assert config.browser.keep_open_after_manual_run is True
    assert config.tasks[0].category_scope.targets == ["50021853", "216502"]
    assert {task.platform for task in load_config(Path("config/tasks.yaml")).tasks if task.enabled} == {"compass"}


def test_default_taobao_root_is_present_but_excluded_until_real_acceptance():
    """Configure the requested root without activating an unverified scheduled task."""
    from compass_collector.runner import select_tasks

    # 默认任务集合保持抖音，显式指定也不能执行仍禁用的淘宝全根任务。
    config = load_config(Path("config/tasks.yaml"))
    task = next(task for task in config.tasks if task.platform == "taobao")
    assert task.category_scope.mode == "all_level1"
    assert task.category_scope.root_category_id == "50025705"
    assert task.rank.page_size == 20
    assert task.date.date_type == "today"
    assert config.browser_for("taobao").profile_dir != config.browser_for("compass").profile_dir
    assert config.browser_for("taobao").webdriver_compatibility is True
    assert config.browser_for("compass").webdriver_compatibility is False
    assert config.browser_for("taobao").persist_session_cookies is True
    assert config.browser_for("compass").persist_session_cookies is False
    assert {task.platform for task in select_tasks(config, None)} == {"compass"}
    with pytest.raises(ValueError, match="enabled task not found"):
        select_tasks(config, task.id)


def test_gui_summary_accepts_selected_and_full_taobao_scopes():
    """GUI construction must not dereference Compass-only category attributes."""
    from compass_collector.gui import category_scope_summary
    from compass_collector.config import TaskConfig

    # 同时覆盖 PoC 指定范围和后续默认全根范围的显示契约。
    task = load_config(Path("config/taobao-poc.yaml")).tasks[0]
    assert category_scope_summary(task) == "50021853, 216502"
    full = TaskConfig(id="taobao_full", platform="taobao", display_name="全根", schedule="0 14 * * *")
    assert category_scope_summary(full) == "cateId=50025705 下全部三级分类"
    # 正式配置增加第二个根，界面必须明确展示追加范围。
    expanded = load_config(Path("config/taobao.yaml")).tasks[0]
    assert "追加 cateId=50016348" in category_scope_summary(expanded)
    assert "2132, 50003949, 50009146" in category_scope_summary(expanded)
    assert "industry_id=5" in category_scope_summary(load_config(Path("config/tasks.yaml")).tasks[0])


def test_production_login_installs_listeners_before_normal_navigation(monkeypatch, tmp_path):
    """Real factory controls must navigate without credential replay and close safely."""
    # 浏览器替身只记录导航和事件注册；生产 login 与 controls 都保持真实。
    from pyee import EventEmitter
    from compass_collector import runner
    from compass_collector.platforms import taobao

    events = []
    context = EventEmitter()
    page = SimpleNamespace(url="about:blank")

    def navigate(url, **kwargs):
        """Keep fake page location consistent with the production navigation path."""
        page.url = url
        events.append((url, tuple(context.event_names())))

    page.goto = navigate
    session = SimpleNamespace(page=page, context=context, close=lambda: events.append("closed"),
                              wait_for_manual_exit=lambda message: events.append("login_wait"))
    monkeypatch.setattr(taobao, "open_browser", lambda config: session)
    # 锁仍使用真实实现，由独立 tmp 目录隔离测试运行文件。
    monkeypatch.setattr(runner, "RUNTIME_ROOT", tmp_path)
    config = load_config(Path("config/taobao-poc.yaml"))
    assert runner.run_login(config, "taobao") == 0
    # 正常导航必须晚于监听安装，不能使用独立 HTTP 重放。
    url, listeners = events[0]
    assert set(listeners) == {"request", "response", "requestfinished", "requestfailed"}
    assert urlsplit(url).hostname == "sycm.taobao.com"
    assert parse_qs(urlsplit(url).query)["dateType"] == ["today"]
    assert "token" not in parse_qs(urlsplit(url).query)
    assert events[1:] == ["login_wait", "closed"]
    assert not context.event_names()


def test_click_timeout_never_repeats_a_possible_completed_action():
    """A timed-out click must fail once rather than issuing duplicate requests."""
    from playwright.sync_api import TimeoutError

    # 点击计数模拟请求已发生但页面确认超时的风险窗口。
    clicks = []

    def click(**kwargs):
        """Record the single attempted click before the timeout."""
        clicks.append(True)
        raise TimeoutError("synthetic timeout")

    locator = SimpleNamespace(count=lambda: 1, is_visible=lambda: True,
                              is_enabled=lambda: True, click=click)
    page = SimpleNamespace(screenshot=lambda **kwargs: b"synthetic")
    with pytest.raises(BrowserOperationError):
        TaobaoBrowserControls(CollectionConfig())._click(page, locator, "test_click")
    assert clicks == [True]


def test_stopped_control_never_navigates():
    """Cancellation must be checked before creating a live page request."""
    # 停止状态不能被导航或默认 Playwright 超时覆盖。
    controls = TaobaoBrowserControls(CollectionConfig(), SimpleNamespace(stop_requested=lambda: True))
    with pytest.raises(CollectionInterruptedError):
        controls.enter(SimpleNamespace(goto=lambda *args, **kwargs: pytest.fail("navigation after stop")))
