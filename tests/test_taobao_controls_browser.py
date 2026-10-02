"""Headless Chrome DOM checks on synthetic HTML, with no Taobao network access."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from compass_collector.browser import open_browser, build_page_error
from compass_collector.config import BrowserConfig, CollectionConfig, TaskConfig
from compass_collector.errors import BrowserOperationError
from compass_collector.models import DiscoveredScope
from compass_collector.platforms.taobao_controls import TaobaoBrowserControls
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# 所有内容通过 set_content 提供；禁止测试访问任何外部站点。
pytestmark = pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1",
                                reason="Requires local Google Chrome")
HTML = """<!doctype html><html><body>
<div class="oui-page-size-select"><button role="combobox" onclick="options.hidden=false">
<span class="ant-select-selection-selected-value">10</span></button></div>
<div id="options" hidden><button role="option" onclick="setSize()">20</button></div>
<button class="item-cate" onclick="showLevel(0)">分类</button>
<div class="common-picker-menu" id="menu" hidden>
<ul class="tree-menu tree-scroll-menu-level-1" id="level1"></ul>
<ul class="tree-menu tree-scroll-menu-level-2" id="level2"></ul>
<ul class="tree-menu tree-scroll-menu-level-3" id="level3"></ul></div>
<div class="ant-pagination-item-active">1</div>
<button class="ant-pagination-next" onclick="next()">下一页</button>
<script>
// 合成状态只验证可见控件操作，不模拟平台接口契约。
window.audit = {sizeChanges:0, selections:[], nextClicks:0};
function setSize(){document.querySelector('.ant-select-selection-selected-value').textContent='20';
audit.sizeChanges++;options.hidden=true;}
function showLevel(level){menu.hidden=false;const target=document.getElementById('level'+(level+1));
target.replaceChildren();
const names = [['洗护清洁剂/卫生巾/纸/香薰'],['香薰用品'],['香薰蜡烛','香薰精油']][level];
for(const name of names){const item=document.createElement('li');item.textContent=name;
if(level<2)item.onmouseover=()=>showLevel(level+1);else item.onclick=()=>{
audit.selections.push(name);menu.hidden=true;
document.querySelector('.ant-pagination-item-active').textContent='1';};target.append(item);}}
function next(){audit.nextClicks++;const active=document.querySelector('.ant-pagination-item-active');
active.textContent=Number(active.textContent)+1;}
</script></body></html>"""


def local_browser_config(**options):
    """Keep production config validation unchanged while hiding synthetic test windows."""
    # 生产配置有意限制可见Chrome；仅合成测试复制对象切换无界面，不扩展用户配置契约。
    return BrowserConfig(**options).model_copy(update={"headless": True})


@pytest.fixture
def page(tmp_path):
    """Create and dispose a temporary account-free Chrome profile."""
    # 独立临时 Profile 与生产账号完全隔离，所有请求都中止。
    # 本地合成页面无须占用用户屏幕，保持Chrome内核而不连续弹出测试窗口。
    session = open_browser(local_browser_config(profile_dir=tmp_path / "profile",
                                               keep_open_after_manual_run=False))
    session.context.route("**/*", lambda route: route.abort())
    try:
        session.page.set_content(HTML)
        yield session.page
    finally:
        session.close()


def test_twenty_rows_persist_across_two_categories_and_three_pages(page):
    """Use real DOM clicks to verify initialization is not repeated per category."""
    # 当前北京时间只驱动控制器日期检查，页面内容全部是本地合成数据。
    controls = TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=2))
    task = TaskConfig(id="taobao_local", platform="taobao", display_name="本地控件", schedule="0 14 * * *")
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    controls.initialize(page, task, day)
    for name in ("香薰蜡烛", "香薰精油"):
        # 同一个真实页面上验证完整路径选择和类别切换复位页码。
        scope = DiscoveredScope(1, "1", ("洗护清洁剂/卫生巾/纸/香薰", "香薰用品", name), {})
        controls.select_scope(page, scope)
        controls.confirm_page(page, 1, 41)
        if name == "香薰蜡烛":
            for page_no in (2, 3):
                controls.next_page(page)
                controls.confirm_page(page, page_no, 41)
    # 直接读取页面的动作计数，避免用 Python 控件替身证明自身实现。
    assert page.evaluate("window.audit") == {
        "sizeChanges": 1, "selections": ["香薰蜡烛", "香薰精油"], "nextClicks": 2,
    }


def test_disabled_next_page_is_not_clicked_and_saves_diagnostics(page):
    """A disabled rendered pager must fail without changing the page."""
    # 超时很短，仅用于验证禁用控件，不引入无意义的长等待。
    controls = TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=0.2))
    page.locator(".ant-pagination-next").evaluate("node => node.classList.add('ant-pagination-disabled')")
    with pytest.raises(BrowserOperationError) as caught:
        controls.next_page(page)
    assert caught.value.failed_step == "taobao_next_page"
    assert caught.value.screenshot
    assert page.evaluate("window.audit.nextClicks") == 0


@pytest.mark.parametrize("ambiguous", [False, True])
def test_category_full_title_matches_truncated_text_and_rejects_ambiguity(page, ambiguous):
    """Use the real site's full title when the visible category name is abbreviated."""
    # 真实50458020的菜单文本有省略号，title仍包含来源接口的完整名称。
    name = "生活用纸 > 平板式/抽取式/挂抽式厕纸"
    page.evaluate("""({name, ambiguous}) => {
        const original=window.showLevel;
        window.showLevel=(level)=>{
            original(level);
            if(level===2){level3.replaceChildren();
                for(let i=0;i<(ambiguous?2:1);i++){
                    const item=document.createElement('li');
                    item.title=name;item.textContent='生活用纸 > ...式/挂抽式厕纸';
                    item.onclick=()=>{audit.selections.push(name);menu.hidden=true;};level3.append(item);
                }
            }
        };
    }""", {"name": name, "ambiguous": ambiguous})
    controls = TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=0.3))
    scope = DiscoveredScope(1, "50458020", ("洗护清洁剂/卫生巾/纸/香薰", "香薰用品", name), {})
    if ambiguous:
        with pytest.raises(BrowserOperationError):
            controls.select_scope(page, scope)
        assert page.evaluate("window.audit.selections") == []
    else:
        controls.select_scope(page, scope)
        assert page.evaluate("window.audit.selections") == [name]


@pytest.mark.parametrize("total", [0, 11, 20])
def test_single_page_ranking_without_page_number_still_confirms_twenty_rows(page, total):
    """The real site omits page-number controls when only one page is needed."""
    # 真实11条分类截图只有20条选择器、没有活动页码；同一规则覆盖空榜和整20条。
    page.locator(".ant-pagination-item-active").evaluate("node => node.remove()")
    page.locator(".ant-pagination-next").evaluate("node => node.remove()")
    page.locator(".ant-select-selection-selected-value").evaluate("node => node.textContent='20'")
    TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=0.2)).confirm_page(page, 1, total)


def test_multi_page_ranking_without_active_page_is_rejected(page):
    """Absence of a pager is acceptable only for an API-confirmed single page."""
    # 多页响应仍必须有明确活动页码，不能因单页兼容而放宽跨页关联。
    page.locator(".ant-pagination-item-active").evaluate("node => node.remove()")
    page.locator(".ant-select-selection-selected-value").evaluate("node => node.textContent='20'")
    with pytest.raises(BrowserOperationError):
        TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=0.2)).confirm_page(page, 1, 41)


def test_completed_page_with_lost_twenty_rows_has_specific_recovery_step(page):
    """Separate a page-size reset from an unknown active-page failure."""
    # 响应匹配20条但实际选择器退回10条，不允许确认或把它归为未知页码错误。
    with pytest.raises(BrowserOperationError) as caught:
        TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=0.2)).confirm_page(page, 1, 41)
    assert caught.value.failed_step == "taobao_page_size_lost"


def test_initialization_waits_for_delayed_page_size_render(page):
    """An authenticated shell may render its selected value after the first half second."""
    # 延迟恢复真实 DOM 节点，模拟页面加载过程中分页内容稍晚出现。
    page.evaluate("""() => {const value=document.querySelector('.ant-select-selection-selected-value');
        const parent=value.parentNode;value.remove();setTimeout(()=>parent.append(value),700);} """)
    controls = TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=2))
    task = TaskConfig(id="taobao_delayed", platform="taobao", display_name="延迟控件", schedule="0 14 * * *")
    controls.initialize(page, task, datetime.now(ZoneInfo("Asia/Shanghai")).date())
    assert page.evaluate("window.audit.sizeChanges") == 1


@pytest.mark.parametrize("completed", [True, False])
def test_page_size_click_timeout_checks_result_without_repeating_selection(page, monkeypatch, completed):
    """A click can time out after selecting twenty; only a confirmed result is accepted."""
    controls = TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=1))
    task = TaskConfig(id="taobao_click", platform="taobao", display_name="本地控件", schedule="0 14 * * *")
    # 真正操作合成DOM后注入点击返回超时，模拟真实截图中已选20且加载仍在继续。
    original_click = controls._click

    def timed_out_click(page, locator, step):
        """Retain real DOM behavior while controlling whether the click completed."""
        if step != "taobao_page_size_twenty":
            return original_click(page, locator, step)
        if completed:
            original_click(page, locator, step)
        raise build_page_error(page, PlaywrightTimeoutError("Synthetic post-click timeout"), failed_step=step)

    monkeypatch.setattr(controls, "_click", timed_out_click)
    if completed:
        controls.initialize(page, task, datetime.now(ZoneInfo("Asia/Shanghai")).date())
    else:
        with pytest.raises(BrowserOperationError):
            controls.initialize(page, task, datetime.now(ZoneInfo("Asia/Shanghai")).date())
    assert page.evaluate("window.audit.sizeChanges") == int(completed)


@pytest.mark.parametrize("compatibility", [False, True])
def test_webdriver_compatibility_applies_before_page_and_child_frame_scripts(tmp_path, compatibility):
    """Check the actual prototype in Chrome while leaving the default mode unchanged."""
    # 两种模式各使用临时 Profile，不接触已登录淘宝目录。
    session = open_browser(local_browser_config(profile_dir=tmp_path / "webdriver-profile",
                                               webdriver_compatibility=compatibility))
    session.context.route("**/*", lambda route: route.abort())
    try:
        session.page.goto("data:text/html,<title>Local webdriver check</title><iframe srcdoc='<p>child</p>'></iframe>")
        session.page.locator("iframe").wait_for()
        # 同时验证直接读取、原型 getter 和实例属性，避免只覆盖一种读取方式。
        source = """() => ({value: String(navigator.webdriver),
            own: Object.hasOwn(navigator, 'webdriver'),
            prototypeValue: String(Object.getOwnPropertyDescriptor(Navigator.prototype,'webdriver').get.call(navigator))})"""
        for frame in session.page.frames:
            # 子 frame 也必须在自身脚本运行前继承上下文初始化脚本。
            result = frame.evaluate(source)
            assert result == {"value": "undefined" if compatibility else "true", "own": False,
                              "prototypeValue": "undefined" if compatibility else "true"}
        assert len(session.page.frames) == 2
    finally:
        session.close()


def test_session_login_survives_normal_profile_restart_and_logout(tmp_path):
    """Exercise login, close, reopen and logout through production Chrome lifecycle."""
    # 本地合成登录以无过期时间 Cookie 鉴权，模拟同一 Profile 重启后再次要求登录。
    # 认证仅来自本地合成站点，重启验证也不弹出账号窗口。
    config = local_browser_config(profile_dir=tmp_path / "login-profile", persist_session_cookies=True)

    def install_local_site(context):
        """Serve a deterministic account-free login site without external traffic."""
        def respond(route):
            """Render the authenticated controls only while the synthetic cookie exists."""
            # 所有请求由本地应答；独立临时 Profile 不涉及真实淘宝账号。
            path = route.request.url.rsplit("/", 1)[-1]
            headers = {"content-type": "text/html"}
            if path == "login":
                headers["set-cookie"] = "synthetic_auth=active; Path=/; HttpOnly; SameSite=Lax"
            elif path == "logout":
                headers["set-cookie"] = "synthetic_auth=; Path=/; Max-Age=0"
            # 登录态与真实页面控制器共用可见分页判断，避免只断言备份文件存在。
            authenticated = "synthetic_auth=active" in route.request.headers.get("cookie", "")
            body = '<div class="oui-page-size-select">20</div>' if authenticated else "请重新登录"
            route.fulfill(status=200, headers=headers, body=body)
        context.route("**/*", respond)

    for index in range(3):
        # 第一次登录、第二次复用并退出登录、第三次验证退出不会被旧备份恢复。
        session = open_browser(config)
        install_local_site(session.context)
        try:
            if index == 0:
                session.page.goto("https://sycm.taobao.com/login")
            session.page.goto("https://sycm.taobao.com/mc/free/market_rank")
            controls = TaobaoBrowserControls(CollectionConfig())
            assert controls.authenticated(session.page) is (index < 2)
            if index == 1:
                session.page.goto("https://sycm.taobao.com/logout")
        finally:
            session.close()
        # 本地凭证只能位于隔离 Profile，POSIX 平台不得向其他用户开放读取权限。
        snapshot = config.profile_dir / ".session-cookies.json"
        assert snapshot.is_file()
        if os.name == "posix":
            assert snapshot.stat().st_mode & 0o777 == 0o600


def test_manual_login_home_transition_waits_for_visible_market_link(page):
    """The login page must remain untouched; navigate once its merchant-home link appears."""
    # 所有导航由本地路由应答，验证真实控件可见性与 URL 边界。
    page.context.unroute("**/*")

    def respond(route):
        """Render a delayed merchant link on home and an authenticated rank shell."""
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=(
            '<span id="market" hidden>市场</span>' if "/portal/home.htm" in route.request.url
            else '<div class="oui-page-size-select">20</div>'))

    page.context.route("**/*", respond)
    controls = TaobaoBrowserControls(CollectionConfig(action_timeout_seconds=2))
    page.goto("https://sycm.taobao.com/custom/login.htm")
    assert controls.resume_after_login(page) is False
    assert "/custom/login.htm" in page.url
    page.goto("https://sycm.taobao.com/portal/home.htm")
    assert controls.resume_after_login(page) is False
    page.locator("#market").evaluate("node => node.hidden=false")
    assert controls.resume_after_login(page) is True
    assert controls.authenticated(page)
