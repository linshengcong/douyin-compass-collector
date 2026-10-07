"""Compass page navigation, authentication, response matching and normalization."""

import json
import random
from datetime import datetime
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from compass_collector.browser import open_browser
from compass_collector.errors import (
    AuthRequiredError,
    CollectionInterruptedError,
    HttpRequestError,
    HttpResponseError,
    ResponseContractError,
)
from compass_collector.models import CategoryDiscoveryResult, DiscoveredScope
from compass_collector.platforms.compass_categories import (
    CATEGORY_TREE_ENDPOINT_PATH,
    parse_category_tree,
)
from compass_collector.platforms.compass_product_rank import (
    build_request_params,
    validate_page_payload,
    parse_page_entries,
    validate_complete_ranking,
)
from compass_collector.platforms.contracts import DiscoveryCapture, PageCapture

# 接口只用于监听页面请求，永远不主动构造 HTTP 请求。
RANK_PATH = "/compass_api/shop/product/product_rank/market_hot_sale"
HOME_URL = "https://compass.jinritemai.com/shop"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def matches_rank_response(response, expected: dict) -> bool:
    """Match the whole safe business query, rejecting duplicates and old scopes."""
    # URL 仅在内存解析，签名及其他认证参数不会被写入审计摘要。
    parts = urlsplit(response.url)
    if parts.hostname != "compass.jinritemai.com" or parts.path != RANK_PATH:
        return False
    # 保留空业务字段，重复值不能冒充一个正确值。
    query = parse_qs(parts.query, keep_blank_values=True)
    return all(query.get(key) == [str(value)] for key, value in expected.items())


def select_scopes(discovery, selection) -> CategoryDiscoveryResult:
    """Resolve every configured target before accepting any ranking pages."""
    # 平台分类参数仅在适配器中解释，共享编排只接收通用范围快照。
    candidates = {
        (item.level1_category_id, f"{item.level2_category_id},{item.category_id}"): item
        for item in discovery.categories
    }
    if selection.mode == "selected":
        # 一项无效就阻止整个任务，不能发布范围被缩减的数据。
        missing = [
            (target.industry_id, target.category_id)
            for target in selection.targets
            if (target.industry_id, target.category_id) not in candidates
        ]
        if missing:
            raise ResponseContractError(
                "Invalid configured category combinations: " + json.dumps(missing),
                category="invalid_configured_category",
            )
        # 指定范围严格按 YAML 顺序执行。
        categories = [
            candidates[(target.industry_id, target.category_id)]
            for target in selection.targets
        ]
    else:
        # 行业限定只筛选已发现的三级节点，沿用原分类树顺序，不下钻四级。
        categories = [
            category
            for category in discovery.categories
            if selection.industry_id is None
            or category.level1_category_id == selection.industry_id
        ]
        if not categories:
            raise ResponseContractError(
                "Configured industry has no collectable third-level categories",
                category="invalid_configured_industry",
            )
    return CategoryDiscoveryResult(
        None,
        None,
        tuple(
            DiscoveredScope(
                index, category.category_id, category.path, category.platform_metadata
            )
            for index, category in enumerate(categories, 1)
        ),
    )


class CompassAdapter:
    """Hide the complete page workflow behind the shared platform interface."""

    def __init__(self, browser, collection, *, manual: bool, control=None):
        """Accept configuration and cancellation without starting Chrome."""
        # 配置和控制器由运行入口创建，会话始终属于同一任务线程。
        self.browser_config = browser
        self.settings = collection
        self.manual = manual
        self.control = control
        self.session = None
        self.page = None
        # 只保存目标接口响应，不记录认证请求头或完整请求历史。
        self.category_responses = []
        self.expected = None
        self.armed_requests = set()
        self.pending = []
        self.category_tree = None
        # 回调只缓存对象，不在 route/response 回调中调用同步网络 API。
        self.responses_by_request = {}
        # 页面切换产生的中间请求也必须结束，避免旧渲染覆盖下一页。
        self.inflight_rank_requests = set()

    def _check_stopped(self) -> None:
        """Raise the shared interruption error at every event-pumping boundary."""
        if self.control is not None and self.control.stop_requested():
            raise CollectionInterruptedError(
                "Collection interrupted", category="interrupted"
            )

    def _pump(self, seconds: float) -> None:
        """Pump Playwright events in short chunks so GUI cancellation stays live."""
        # 事件循环不能使用 time.sleep，否则响应监听和人工恢复都会停滞。
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            self._check_stopped()
            self.page.wait_for_timeout(
                min(100, max(1, (deadline - monotonic()) * 1000))
            )

    def _on_request(self, request) -> None:
        """Arm only requests created after the current action was registered."""
        if urlsplit(request.url).path == RANK_PATH:
            self.inflight_rank_requests.add(request)
        if self.expected is not None and matches_rank_response(request, self.expected):
            self.armed_requests.add(request)

    def _on_response(self, response) -> None:
        """Remember relevant response objects without a nested sync API call."""
        # response 事件可能早于正文下载完成，完成事件再投递给采集器。
        if (
            response.request in self.armed_requests
            or urlsplit(response.url).path == CATEGORY_TREE_ENDPOINT_PATH
        ):
            self.responses_by_request[response.request] = response

    def _on_finished(self, request) -> None:
        """Deliver only completed bodies using the response-event object cache."""
        self.inflight_rank_requests.discard(request)
        response = self.responses_by_request.pop(request, None)
        if response is None:
            return
        if urlsplit(response.url).path == CATEGORY_TREE_ENDPOINT_PATH:
            self.category_responses.append(response)
        if request in self.armed_requests:
            self.pending.append(response)
            self.armed_requests.discard(request)

    def _on_failed(self, request) -> None:
        """Discard aborted requests; the finite response wait controls retry."""
        self.inflight_rank_requests.discard(request)
        self.responses_by_request.pop(request, None)
        self.armed_requests.discard(request)

    def _click(self, locator) -> None:
        """Perform bounded locator actions while supporting user cancellation."""
        # 小片段 DOM 等待避免单次默认 30 秒阻塞停止按钮。
        deadline = monotonic() + self.settings.action_timeout_seconds
        while monotonic() < deadline:
            self._check_stopped()
            try:
                locator.click(timeout=1000)
                return
            except PlaywrightTimeoutError:
                self._pump(0.1)
        raise HttpRequestError("Page action timed out", category="page_action_timeout")

    def _home_ready(self) -> bool:
        """Recognize the observed authenticated home entry without reading cookies."""
        return self.page.get_by_text("查看同行榜单", exact=True).is_visible()

    def _await_manual_auth(self, ready) -> None:
        """Wait only in manual runs; schedulers never hold an unattended challenge."""
        if not self.manual:
            raise AuthRequiredError(
                "Manual authentication required", category="auth_required"
            )
        # 用户看到的提示只有处理步骤，不包含账号或认证值。
        print("请在 Chrome 中完成登录或人工验证，程序将限时等待。", flush=True)
        deadline = monotonic() + self.settings.manual_auth_wait_seconds
        while monotonic() < deadline:
            self._check_stopped()
            if ready():
                return
            self._pump(0.1)
        raise AuthRequiredError(
            "Authentication wait expired", category="auth_wait_timeout"
        )

    def open_session(self, *, login_only: bool = False) -> None:
        """Navigate through the observed home popup after installing listeners."""
        if self.session is not None:
            return
        self.session = open_browser(self.browser_config)
        self.page = self.session.page
        self.session.context.on("request", self._on_request)
        self.session.context.on("response", self._on_response)
        self.session.context.on("requestfinished", self._on_finished)
        self.session.context.on("requestfailed", self._on_failed)
        self.page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=self.settings.response_timeout_seconds * 1000,
        )
        if login_only:
            return
        # 正常页面初始化和人工登录分开等待，定时任务也允许正常加载。
        deadline = monotonic() + self.settings.action_timeout_seconds
        while not self._home_ready() and monotonic() < deadline:
            if "login" in urlsplit(self.page.url).path:
                break
            self._pump(0.1)
        if not self._home_ready():
            self._await_manual_auth(self._home_ready)
        # 入口创建新标签，所有新标签响应也由上下文监听。
        # 比较点击前标签集合，避免旧标签被误认为本次新窗口。
        existing_pages = set(self.session.context.pages)
        self._click(self.page.get_by_text("查看同行榜单", exact=True))
        deadline = monotonic() + self.settings.action_timeout_seconds
        while (
            not (set(self.session.context.pages) - existing_pages)
            and monotonic() < deadline
        ):
            self._pump(0.1)
        if not (set(self.session.context.pages) - existing_pages):
            raise HttpRequestError(
                "Ranking popup missing", category="page_action_timeout"
            )
        self.page = next(
            page for page in self.session.context.pages if page not in existing_pages
        )
        self.session.page = self.page
        self._click(self.page.get_by_text("商品榜单", exact=True))

    def discover_scopes(self, task, *, full_catalog=False) -> DiscoveryCapture:
        """Read the page-generated category tree and validate configured IDs."""
        self.open_session()
        # 新任务使用已建立的分类树快照，不反复读取认证信息。
        deadline = monotonic() + self.settings.response_timeout_seconds
        # 正常分类树的契约变化应报告契约错误，不误报登录失效。
        last_contract_error = None
        # 分类发现最多进行一次人工恢复，恢复失败直接结束任务。
        auth_recovered = False
        while True:
            self._check_stopped()
            for response in reversed(self.category_responses):
                try:
                    # 错误分类响应不能作为合法分类树，等待正常页面响应。
                    payload = response.json()
                    discovery = parse_category_tree(payload)
                except ValueError:
                    continue
                except ResponseContractError as error:
                    if isinstance(payload, dict) and payload.get("st") == 0:
                        error.discovery_payload = payload
                        raise
                    if isinstance(payload, dict) and payload.get("st") in (
                        10012,
                        10008,
                    ):
                        last_contract_error = AuthRequiredError(
                            "Category discovery requires authentication",
                            category="auth_required",
                        )
                        if not self.manual:
                            raise last_contract_error
                    else:
                        last_contract_error = error
                    continue
                self.category_tree = payload
                try:
                    scopes = discovery if full_catalog else select_scopes(discovery, task.category_scope)
                except ResponseContractError as error:
                    error.discovery_payload = payload
                    raise
                return DiscoveryCapture(scopes, payload)
            self._pump(0.1)
            if monotonic() < deadline:
                continue
            if isinstance(last_contract_error, AuthRequiredError):
                if auth_recovered or not self.manual:
                    raise last_contract_error
                # 旧认证响应不能继续污染恢复后的发现；保留上下文和监听。
                self.category_responses.clear()
                self._recover_auth()
                auth_recovered = True
                deadline = monotonic() + self.settings.response_timeout_seconds
                last_contract_error = None
                continue
            if last_contract_error is not None:
                raise last_contract_error
            raise AuthRequiredError(
                "No authenticated category response", category="auth_required"
            )

    def _set_filters(self, task) -> None:
        """Set supported Compass fields using real page controls."""
        self._click(self.page.get_by_text("实时", exact=True))
        # 品牌不限必须限定到品牌筛选组。
        brand_group = self.page.get_by_text("知名品牌", exact=True).locator("..")
        self._click(
            brand_group.get_by_text(
                "不限" if task.filters.brand_type == -1 else "非知名品牌", exact=True
            )
        )
        # 价格不限和品牌不限分组匹配，不能按父节点层数猜测。
        price_group = self.page.locator('div[class^="tagList-"]').filter(
            has=self.page.get_by_text("自定义", exact=True)
        )
        if task.filters.price_bin == "不限":
            self._click(price_group.get_by_text("不限", exact=True))
        else:
            self._click(price_group.get_by_text("自定义", exact=True))
            # 自定义价格只填写已确认下限，必须以实际发出的参数作为验收依据。
            # 对话框先完成挂载再填写，瞬时 count 不能判断控件是否缺失。
            dialog = self.page.get_by_role("dialog")
            minimum = dialog.get_by_placeholder("输入最小值", exact=True)
            deadline = monotonic() + self.settings.action_timeout_seconds
            while monotonic() < deadline:
                self._check_stopped()
                try:
                    minimum.fill("10001", timeout=1000)
                    break
                except PlaywrightTimeoutError:
                    self._pump(0.1)
            else:
                raise ResponseContractError(
                    "Custom price input unavailable",
                    category="custom_price_control_unverified",
                )
            self._click(dialog.get_by_text("确定", exact=True))

    def _select_scope(self, scope) -> None:
        """Select names resolved from IDs, refreshing an already-selected category."""
        # 页面名称只来自已验证分类树，不允许配置手工替换名称。
        selector = self.page.locator(".aurora-select-content-value").first
        current_path = selector.inner_text(
            timeout=self.settings.action_timeout_seconds * 1000
        )
        self._click(selector)
        self._click(self.page.get_by_text(scope.path[0], exact=True))
        self._click(self.page.get_by_text(scope.path[1], exact=True))
        if current_path == " / ".join(scope.path):
            # 当前分类不一定再次触发请求，先切回该二级的全部再重新选择。
            self._click(self.page.get_by_text("全部", exact=True).last)
            self._click(selector)
            self._click(self.page.get_by_text(scope.path[0], exact=True))
            self._click(self.page.get_by_text(scope.path[1], exact=True))
        self._click(self.page.get_by_text(scope.path[2], exact=True))

    def _wait_response(self, expected):
        """Wait for a completed response tied to a newly created request."""
        # 日期冻结后不能跨天把新实时榜单算作旧任务结果。
        deadline = monotonic() + self.settings.response_timeout_seconds
        while monotonic() < deadline:
            self._check_stopped()
            if self.pending:
                return self.pending.pop(0)
            if "login" in urlsplit(self.page.url).path:
                raise AuthRequiredError("Login state expired", category="auth_required")
            self._pump(0.1)
        raise HttpRequestError(
            "Ranking response timed out", category="page_response_timeout"
        )

    def _confirm_page(self, page_no, *, allow_empty=False):
        """Require the visible page to settle before another navigation action."""
        # 响应完成可能早于 React 更新，下一步必须等待活动页。
        active = self.page.locator(".aurora-pagination-item-active")
        deadline = monotonic() + self.settings.action_timeout_seconds
        while monotonic() < deadline:
            self._check_stopped()
            if active.count() == 1 and active.inner_text(timeout=500).strip() == str(
                page_no
            ):
                return
            if allow_empty and active.count() == 0:
                return
            self._pump(0.1)
        raise ResponseContractError(
            "Visible page differs from captured response",
            category="visible_page_mismatch",
        )

    def _payload(self, response):
        """Validate transport and authentication before page-contract parsing."""
        if response.status != 200:
            raise HttpResponseError(
                "Ranking HTTP status failed",
                category="ranking_http_error",
                status_code=response.status,
            )
        try:
            # 正文下载完成后读取，解析异常不能泄露平台响应或完整 URL。
            payload = response.json()
        except ValueError as error:
            raise HttpResponseError(
                "Ranking response is not JSON", category="invalid_json"
            ) from error
        if not isinstance(payload, dict):
            raise ResponseContractError(
                "Ranking root is not an object", category="invalid_contract"
            )
        if payload.get("st") in (10012, 10008):
            raise AuthRequiredError(
                "Ranking requires authentication", category="auth_required"
            )
        return payload

    def _capture(self, task, scope, business_date, page_no, *, restore=False):
        """Rebuild category then filters; category changes reset Compass prices."""
        if datetime.now(TIMEZONE).date() != business_date:
            raise CollectionInterruptedError(
                "Business date changed", category="business_date_changed"
            )
        # 监听先于动作，request 对象身份隔离晚到旧响应。
        expected = build_request_params(task, scope, business_date, page_no)
        self.armed_requests.clear()
        self.pending.clear()
        try:
            if page_no == 1 or restore:
                # 类目动作会重置价格；先接受用于定位页面的响应，再设置最终业务条件。
                self._click(self.page.get_by_text("实时", exact=True))
                first_params = build_request_params(task, scope, business_date, 1)
                self.expected = {
                    key: value
                    for key, value in first_params.items()
                    if key not in ("brand_type", "price_bin")
                }
                self._select_scope(scope)
                selected_response = self._wait_response(self.expected)
                self._confirm_page(1, allow_empty=True)
                self.expected = first_params
                self.armed_requests.clear()
                self.pending.clear()
                self._set_filters(task)
                self._pump(0.1)
                # 已处于全部目标条件时，控件点击不一定产生新请求。
                if (
                    not self.armed_requests
                    and not self.pending
                    and matches_rank_response(selected_response, first_params)
                ):
                    response = selected_response
                else:
                    response = self._wait_response(first_params)
                if page_no > 1:
                    # 恢复页只核对、不持久化，最终只返回尚未完成的一页。
                    for restored_page in range(2, page_no + 1):
                        restored_payload = self._payload(response)
                        validate_page_payload(
                            restored_payload,
                            requested_page=restored_page - 1,
                            expected_total=None,
                        )
                        self._confirm_page(restored_page - 1)
                        self.expected = build_request_params(
                            task, scope, business_date, restored_page
                        )
                        self.armed_requests.clear()
                        self.pending.clear()
                        self._next_page()
                        response = self._wait_response(self.expected)
                return response, expected
            self.expected = expected
            self._confirm_page(page_no - 1)
            self._next_page()
            return self._wait_response(expected), expected
        finally:
            self.expected = None

    def _recover_auth(self):
        """Reenter the observed home path after a bounded manual recovery."""
        # 回到首页重新确认登录，不把仍可见的旧榜单当成登录已恢复。
        if not self.manual:
            raise AuthRequiredError(
                "Manual authentication required", category="auth_required"
            )
        self.page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=self.settings.response_timeout_seconds * 1000,
        )
        self._await_manual_auth(self._home_ready)
        # 保留原 context 和监听，只重建平台页面流程。
        existing_pages = set(self.session.context.pages)
        self._click(self.page.get_by_text("查看同行榜单", exact=True))
        deadline = monotonic() + self.settings.action_timeout_seconds
        while (
            not (set(self.session.context.pages) - existing_pages)
            and monotonic() < deadline
        ):
            self._pump(0.1)
        new_pages = set(self.session.context.pages) - existing_pages
        if not new_pages:
            raise AuthRequiredError(
                "Recovery entry unavailable", category="auth_required"
            )
        self.page = next(iter(new_pages))
        self.session.page = self.page
        self._click(self.page.get_by_text("商品榜单", exact=True))

    def _settle_ranking(self):
        """Finish intermediate page requests before a subsequent pagination click."""
        # 完整业务匹配只决定可采数据；页面状态还受其他中间请求影响。
        deadline = monotonic() + self.settings.response_timeout_seconds
        self._pump(0.1)
        while self.inflight_rank_requests and monotonic() < deadline:
            self._pump(0.1)
        if self.inflight_rank_requests:
            raise HttpRequestError(
                "Page queries did not settle", category="page_response_timeout"
            )
        self._pump(0.1)

    def _next_page(self) -> None:
        """Click existing pagination, scrolling up once only if it is absent."""
        # 分页存在时不进行恢复滚动，避免每页反复移动页面。
        pagination = self.page.locator(".aurora-pagination")
        if pagination.count() == 0 or not pagination.is_visible():
            # 按用户要求，仅缺少分页时向上滚一次，禁止每页强制滚动。
            self.page.mouse.wheel(0, -800)
            self._pump(self.settings.scroll_settle_seconds)
            deadline = monotonic() + self.settings.action_timeout_seconds
            while (
                pagination.count() == 0 or not pagination.is_visible()
            ) and monotonic() < deadline:
                self._pump(0.1)
            if pagination.count() == 0 or not pagination.is_visible():
                raise HttpRequestError(
                    "Pagination unavailable after one scroll",
                    category="pagination_missing",
                )
        # 检查实际按钮的命中区域，整条分页可见并不代表按钮能被点击。
        next_button = self.page.locator(
            '.aurora-pagination-next[aria-disabled="false"] button:not([disabled])'
        )
        # 中心可能被浮动助手或覆盖式滚动条遮挡，优先使用其他有效点。
        point = self._pagination_click_point(next_button)
        if point is None and next_button.count():
            next_button.evaluate(
                "el => el.scrollIntoView({block:'center', inline:'center', behavior:'instant'})",
                timeout=1000,
            )
            self._pump(self.settings.scroll_settle_seconds)
        # 定位仅执行一次；后续只等待可点击状态，不循环滚动或盲目点击。
        deadline = monotonic() + self.settings.action_timeout_seconds
        while point is None:
            self._check_stopped()
            point = self._pagination_click_point(next_button)
            if point is not None:
                break
            if monotonic() >= deadline:
                raise HttpRequestError(
                    "Next page button is obstructed", category="pagination_obstructed"
                )
            self._pump(0.1)
        self._check_stopped()
        # 鼠标点击保留已核对的坐标，避免 locator 的自动滚动再次改变位置。
        self.page.mouse.click(point["x"], point["y"])

    def _pagination_click_point(self, button):
        """Find an unobstructed physical click point inside the enabled next button."""
        if button.count() == 0:
            return None
        return button.evaluate(
            """el => {
                // 只在当前可见布局内寻找坐标，不修改页面或绕过遮挡。
                const rect = el.getBoundingClientRect();
                if (!rect.width || !rect.height) return null;
                // 中心优先，边内侧候选点用于避开覆盖式滚动条。
                for (const dx of [0.5, 0.2, 0.8]) {
                    for (const dy of [0.5, 0.2, 0.8]) {
                        // 每个候选点必须处于视口内，且实际命中按钮或其子节点。
                        const x = rect.x + rect.width * dx;
                        const y = rect.y + rect.height * dy;
                        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
                        const hit = document.elementFromPoint(x, y);
                        if (hit === el || el.contains(hit)) return {x, y};
                    }
                }
                return null;
            }""",
            timeout=1000,
        )

    def collect_scope(self, task, scope, business_date):
        """Yield normalized pages, then validate the complete platform ranking."""
        # 完整性校验由平台负责，不把罗盘排名规则强加给未来平台。
        entries = []
        total = None
        target_pages = 1
        page_no = 1
        while page_no <= target_pages:
            # 网络超时重试次数与人工恢复次数分别限制，避免无限等待。
            auth_recovered = False
            for attempt in range(self.settings.network_retry_attempts + 1):
                try:
                    response, params = self._capture(
                        task, scope, business_date, page_no, restore=attempt > 0
                    )
                    payload = self._payload(response)
                    break
                except AuthRequiredError:
                    if auth_recovered:
                        raise
                    self._recover_auth()
                    auth_recovered = True
                    response, params = self._capture(
                        task, scope, business_date, page_no, restore=True
                    )
                    payload = self._payload(response)
                    break
                except (HttpRequestError, PlaywrightTimeoutError) as error:
                    if attempt == self.settings.network_retry_attempts:
                        if isinstance(error, HttpRequestError):
                            raise
                        raise HttpRequestError(
                            "Page operation timed out", category="page_action_timeout"
                        ) from error
                    self._pump(2**attempt)
            # 原始数据先验证，再交给共享编排进行原子审计保存。
            contract = validate_page_payload(
                payload, requested_page=page_no, expected_total=total
            )
            total = contract.api_total
            target_pages = contract.target_page_count
            captured_at = datetime.now(TIMEZONE)
            page_entries = tuple(
                parse_page_entries(payload, page_no=page_no, captured_at=captured_at)
            )
            # 响应 body 完成后再次检查日期，跨午夜结果不能写入旧日期。
            if captured_at.date() != business_date:
                raise CollectionInterruptedError(
                    "Business date changed", category="business_date_changed"
                )
            entries.extend(page_entries)
            # 等待中间请求全部完成，防止它们随后把页面覆盖回第一页。
            self._settle_ranking()
            # 分页 DOM 与响应必须一致；空榜单没有分页控件。
            # 罗盘在单页结果中隐藏整个分页条，请求页码仍必须为第一页。
            self._confirm_page(page_no, allow_empty=total <= 10 and page_no == 1)
            yield PageCapture(
                page_no, total, target_pages, captured_at, page_entries, payload, params
            )
            page_no += 1
            self._pump(
                random.uniform(
                    self.settings.request_interval_seconds.min,
                    self.settings.request_interval_seconds.max,
                )
            )
        validate_complete_ranking(entries, api_total=total)

    def close(self) -> None:
        """Release listeners and Chrome even after partial session initialization."""
        if self.session is not None:
            self.session.close()
            self.session = None
