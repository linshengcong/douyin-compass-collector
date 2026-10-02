"""Visible Taobao controls; selector compatibility awaits live-site acceptance."""

from datetime import datetime
from time import monotonic
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from compass_collector.browser import build_page_error
from compass_collector.errors import BrowserOperationError, CollectionInterruptedError

# Ant 控件约定仅用于首轮兼容实现；实际接口白名单仍是采集结果的接受边界。
ENTRY = "https://sycm.taobao.com/mc/free/market_rank"


class TaobaoBrowserControls:
    """Operate rendered controls with bounded waits and cooperative cancellation."""

    def __init__(self, settings, control=None):
        """Keep timeout policy without starting Chrome or reading credentials."""
        # 控件和适配器共用停止控制；不保存页面或跨任务状态。
        self.settings = settings
        self.control = control

    def _check(self):
        """Honor stop requests between every short DOM wait."""
        if self.control is not None and self.control.stop_requested():
            raise CollectionInterruptedError("Collection interrupted", category="interrupted")

    def _wait(self, page, step, operation):
        """Retry DOM readiness only, never repeat a completed click."""
        # 每次 action 最多占用半秒，长超时期间仍支持 GUI 停止。
        deadline = monotonic() + self.settings.action_timeout_seconds
        while monotonic() < deadline:
            self._check()
            try:
                if operation():
                    return
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(100)
        raise build_page_error(page, RuntimeError("Control unavailable"), failed_step=step)

    def _click(self, page, locator, step):
        """Require exactly one visible target before clicking once."""
        self._wait(page, step, lambda: locator.count() == 1
                   and locator.is_visible() and locator.is_enabled())
        # 点击可能已触发请求后才超时，所以绝不在控件层重试点击。
        self._check()
        try:
            locator.click(timeout=500)
        except PlaywrightTimeoutError as error:
            raise build_page_error(page, error, failed_step=step) from error

    def enter(self, page):
        """Navigate through the normal ranking page with a fresh Beijing date."""
        # 不复制 PoC 日期、签名或 cookie；导航产生的请求由已安装监听接收。
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        query = urlencode({"activeKey": "item", "dateRange": f"{day}|{day}",
                           "dateType": "today", "cateId": "50025705", "cateFlag": "1"})
        self._check()
        try:
            page.goto(f"{ENTRY}?{query}", wait_until="domcontentloaded",
                      timeout=self.settings.action_timeout_seconds * 1000)
            if urlsplit(page.url).path == "/portal/home.htm":
                # 真实冷启动会先回到已登录首页；等待市场导航渲染后再进入一次榜单。
                market = page.get_by_text("市场", exact=True)
                self._wait(page, "taobao_home_ready", lambda:
                           market.count() == 1 and market.is_visible())
                page.goto(f"{ENTRY}?{query}", wait_until="domcontentloaded",
                          timeout=self.settings.action_timeout_seconds * 1000)
        except PlaywrightTimeoutError as error:
            raise build_page_error(page, error, failed_step="taobao_enter") from error

    def authenticated(self, page):
        """Require the ranking origin and visible paging control, not a cookie."""
        # 登录跳转与商品页不能被当作已认证排行页。
        parts = urlsplit(page.url)
        paging = page.locator(".oui-page-size-select")
        return (parts.hostname == "sycm.taobao.com" and parts.path == "/mc/free/market_rank"
                and paging.count() == 1 and paging.is_visible())

    def resume_after_login(self, page):
        """Leave only a fully rendered merchant home, without navigating on login pages."""
        # 人工登录结束可能落在首页；等待真实市场入口出现再导航，不使用固定秒数。
        parts = urlsplit(page.url)
        market = page.get_by_text("市场", exact=True)
        if (parts.hostname != "sycm.taobao.com" or parts.path != "/portal/home.htm"
                or market.count() != 1 or not market.is_visible()):
            return False
        self.enter(page)
        return True

    def authentication_pending(self, page):
        """Allow rank/home rendering time without delaying a known login redirect."""
        # 只等待已观察的两个应用落点；外部登录页及 custom/login 不冒充加载中。
        parts = urlsplit(page.url)
        return (parts.hostname == "sycm.taobao.com"
                and parts.path in {"/mc/free/market_rank", "/portal/home.htm"})

    def initialize(self, page, task, business_date):
        """Confirm current-day entry and choose twenty rows once per session."""
        # 日期入口在 enter 中选择；实际 dateType 与 dateRange 仍逐页严格验证。
        if datetime.now(ZoneInfo("Asia/Shanghai")).date() != business_date:
            raise CollectionInterruptedError("Business date changed", category="business_date_changed")
        # 已观察到的分页容器内选择框必须唯一，禁止误点分类选择框。
        size = page.locator(".oui-page-size-select")
        # 容器先出现不意味着选中值已渲染，等待必须使用完整动作预算。
        selected = size.locator(".ant-select-selection-selected-value")
        self._wait(page, "taobao_page_size_ready", lambda:
                   size.count() == 1 and selected.count() == 1 and selected.is_visible())
        if selected.inner_text(timeout=500).strip() != "20":
            self._click(page, size.get_by_role("combobox"), "taobao_page_size_open")
            try:
                self._click(page, page.get_by_role("option", name="20", exact=True), "taobao_page_size_twenty")
            except BrowserOperationError as error:
                # 实际点击可能已选20、但页面加载令click返回超时；只等待结果，绝不重复点击。
                if error.failed_step != "taobao_page_size_twenty" or error.exception_type != "TimeoutError":
                    raise
        self._wait(page, "taobao_page_size_confirm", lambda:
                   selected.count() == 1 and selected.inner_text(timeout=500).strip() == "20")

    def select_scope(self, page, scope):
        """Choose three source names in the visible category cascade."""
        # 真实页面是自定义三列菜单：悬停一级/二级展开子列，只有三级点击提交。
        self._click(page, page.locator(".item-cate:visible"), "taobao_category_open")
        for level, name in enumerate(scope.path, start=1):
            # 按列及分类接口全名定位，避免误点商品文本或另一列同名分类。
            menu_item = page.locator(
                f".common-picker-menu:visible .tree-scroll-menu-level-{level}:visible"
            ).get_by_text(name, exact=True)
            if level == 3:
                # 淘宝长三级名称的DOM文本含省略号；完整title仍与来源分类名称一致。
                # 合并精确文本兼容短名称，_click仍要求唯一目标，不能用first掩盖歧义。
                menu_item = menu_item.or_(page.locator(
                    ".common-picker-menu:visible .tree-scroll-menu-level-3:visible"
                ).get_by_title(name, exact=True))
                self._click(page, menu_item, "taobao_category_select")
            else:
                self._wait(page, "taobao_category_expand", lambda:
                           menu_item.count() == 1 and menu_item.is_visible())
                self._check()
                try:
                    menu_item.hover(timeout=500)
                except PlaywrightTimeoutError as error:
                    raise build_page_error(page, error, failed_step="taobao_category_expand") from error

    def next_page(self, page):
        """Click the enabled pagination next control without changing page size."""
        self._click(page, page.locator(".ant-pagination-next:visible:not(.ant-pagination-disabled)"),
                    "taobao_next_page")

    def confirm_page(self, page, page_no, total):
        """Verify active page and persistent twenty-row mode after response completion."""
        # recordCount/条数由解析器核验；DOM 只负责确认请求对应当前可见页。
        active = page.locator(".ant-pagination-item-active:visible")
        size = page.locator(".oui-page-size-select .ant-select-selection-selected-value")
        # 真实单页分类（已观察11条）不渲染页码；API总数与请求身份已明确唯一第一页。
        single_page = total <= 20 and page_no == 1
        # 页大小丢失是已知可重建状态，单独分类；未知活动页错误不放宽为通用重试。
        self._wait(page, "taobao_page_size_lost", lambda:
                   size.count() == 1 and size.inner_text(timeout=500).strip() == "20")
        self._wait(page, "taobao_active_page", lambda:
                   ((active.count() == 1 and active.inner_text(timeout=500).strip() == str(page_no))
                    or (single_page and active.count() == 0)))
