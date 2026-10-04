"""Taobao collection orchestration; real page controls remain a verified seam."""

import random
from datetime import datetime
from time import monotonic
from typing import Protocol
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from compass_collector.browser import open_browser
from compass_collector.errors import AuthRequiredError, BrowserOperationError, CollectionInterruptedError, HttpRequestError, HttpResponseError, ResponseContractError
from compass_collector.platforms.contracts import DiscoveryCapture, PageCapture
from compass_collector.platforms.taobao_capture import TaobaoResponseCapture, read_completed_payload
from compass_collector.platforms.taobao_categories import parse_category_tree
from compass_collector.platforms.taobao_product_rank import (
    build_expected_params, parse_page_entries, validate_complete_ranking, validate_page_payload,
)

# 所有时间检查与现有批次使用同一北京时间日期。
TIMEZONE = ZoneInfo("Asia/Shanghai")


class TaobaoPageControls(Protocol):
    """Require visible UI operations and checks, never authenticated HTTP replay."""

    def enter(self, page) -> None:
        """Navigate to the ranking entry with listeners already installed."""

    def authenticated(self, page) -> bool:
        """Check the actual page rather than cookies or a stale table."""

    def resume_after_login(self, page) -> bool:
        """Return to ranking once the actual merchant home has loaded."""

    def authentication_pending(self, page) -> bool:
        """Distinguish an unfinished rank/home shell from a login redirect."""

    def initialize(self, page, task, business_date) -> None:
        """Select today and twenty-row mode once for a fresh page session."""

    def select_scope(self, page, scope) -> None:
        """Select the complete source path, generating a fresh first-page request."""

    def next_page(self, page) -> None:
        """Click one enabled next-page control without resetting page size."""

    def confirm_page(self, page, page_no, total) -> None:
        """Require the visible active page and twenty-row selection to agree."""


class TaobaoAdapter:
    """Own serial capture, cancellation, fresh category attempts and full-rank checks."""

    def __init__(self, browser, collection, *, manual, controls: TaobaoPageControls, control=None):
        """Accept a page-control implementation without starting a browser."""
        # 页面控件入口必须显式提供；未验证选择器不能静默注册为可用生产实现。
        self.browser_config = browser
        self.settings = collection
        self.manual = manual
        self.controls = controls
        self.control = control
        # 会话、监听和初始化状态均限于当前串行任务。
        self.session = None
        self.page = None
        self.capture = None
        self.initialized = False
        # 分类异常后的下一次采集必须重新进入榜单页面。
        self.needs_reset = False

    def _check(self, business_date=None) -> None:
        """Keep both cooperative stop and midnight interruption at every boundary."""
        if self.control is not None and self.control.stop_requested():
            raise CollectionInterruptedError("Collection interrupted", category="interrupted")
        if business_date is not None and datetime.now(TIMEZONE).date() != business_date:
            raise CollectionInterruptedError("Business date changed", category="business_date_changed")

    def _pump(self, seconds, business_date=None) -> None:
        """Pump Playwright events in short chunks; never block with time.sleep."""
        # 使用 monotonic 控制超时，墙上时间只负责业务日期。
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            self._check(business_date)
            self.page.wait_for_timeout(min(100, max(1, (deadline - monotonic()) * 1000)))

    def _authenticate(self) -> None:
        """Allow bounded manual login only when the execution mode permits it."""
        if self.controls.authenticated(self.page):
            return
        # DOMContentLoaded 不等于分页渲染完成；无人值守也必须等待已登录页面就绪。
        readiness_deadline = monotonic() + self.settings.action_timeout_seconds
        resumed = False
        while (self.controls.authentication_pending(self.page)
               and monotonic() < readiness_deadline):
            self._check()
            if not resumed and self.controls.resume_after_login(self.page):
                resumed = True
            self._pump(0.1)
            if self.controls.authenticated(self.page):
                return
        if not self.manual:
            raise AuthRequiredError("Manual authentication required", category="auth_required")
        print("请在淘宝独立 Chrome 中完成登录或人工验证，程序将限时等待。", flush=True)
        # 人工恢复不读取或迁移其他浏览器的登录信息。
        deadline = monotonic() + self.settings.manual_auth_wait_seconds
        # 本次人工恢复最多一次从商家首页回到榜单，避免自动导航打断登录过程。
        while monotonic() < deadline:
            self._check()
            if self.controls.authenticated(self.page):
                return
            if not resumed and self.controls.resume_after_login(self.page):
                resumed = True
            self._pump(0.1)
        raise AuthRequiredError("Authentication wait expired", category="auth_wait_timeout")

    def open_session(self, *, login_only=False) -> None:
        """Attach listeners before the first navigation in an independent profile."""
        if self.session is not None:
            return
        self.session = open_browser(self.browser_config)
        self.page = self.session.page
        self.capture = TaobaoResponseCapture(self.page)
        self.capture.attach(self.session.context)
        self.controls.enter(self.page)
        if not login_only:
            self._authenticate()

    def discover_scopes(self, task) -> DiscoveryCapture:
        """Return a fully validated source tree before any category is collected."""
        self.open_session()
        # 分类来源仅来自当前页面的完整请求；没有材料时明确超时。
        deadline = monotonic() + self.settings.response_timeout_seconds
        while monotonic() < deadline:
            self._check()
            response = self.capture.category_response
            if response is not None:
                payload = read_completed_payload(response)
                try:
                    discovery = parse_category_tree(payload, task.category_scope)
                except ResponseContractError as error:
                    error.discovery_payload = payload
                    raise
                return DiscoveryCapture(discovery, payload)
            self._pump(0.1)
        raise HttpRequestError("Taobao category response timed out", category="page_response_timeout")

    def _initialize(self, task, business_date, *, restore=False) -> None:
        """Restore a fresh page only on recovery, preserving twenty rows otherwise."""
        self._check(business_date)
        if restore:
            # 页面重建后重新初始化20条，正常分类切换不会执行此路径。
            self.capture.disarm()
            self.initialized = False
            self.controls.enter(self.page)
            self._authenticate()
        if not self.initialized:
            self.controls.initialize(self.page, task, business_date)
            self.initialized = True

    def _action(self, task, scope, business_date, page_no, action):
        """Arm a single UI action, then require completed body and visible page."""
        self._check(business_date)
        # expected 白名单不含签名和认证；代次只对本次点击有效。
        params = build_expected_params(task, scope, business_date, page_no)
        generation = self.capture.arm(params)
        try:
            # 分类点击可能已发出请求而click仍返回超时；先验证同一代次的响应，绝不重复点击。
            # 没有完整匹配响应时保留原错误，交由分类补采处理。
            action_timeout = None
            try:
                action()
            except BrowserOperationError as error:
                if (error.category != "browser_page_error"
                        or error.failed_step != "taobao_category_select"
                        or error.exception_type != "TimeoutError"):
                    raise
                action_timeout = error
            deadline = monotonic() + self.settings.response_timeout_seconds
            while monotonic() < deadline:
                self._check(business_date)
                if not self.controls.authenticated(self.page):
                    raise AuthRequiredError("Taobao login expired", category="auth_required")
                response = self.capture.take(generation)
                if response is not None:
                    payload = read_completed_payload(response)
                    contract = validate_page_payload(payload, requested_page=page_no)
                    self.controls.confirm_page(self.page, page_no, contract.api_total)
                    # DOM 等待也会泵入自动刷新；确认后再查歧义，不能只检查响应刚到时。
                    self.capture.ensure_action(generation)
                    self._check(business_date)
                    return payload, params
                self._pump(0.1, business_date)
            if action_timeout is not None:
                raise action_timeout
            raise HttpRequestError("Taobao ranking response timed out", category="page_response_timeout")
        finally:
            self.capture.disarm()

    def _capture_page(self, task, scope, business_date, page_no):
        """每页执行一次 UI 动作，不回放或即时重试失败页。"""
        self._initialize(task, business_date)
        if page_no == 1:
            return self._action(task, scope, business_date, page_no,
                                lambda: self.controls.select_scope(self.page, scope))
        return self._action(task, scope, business_date, page_no,
                            lambda: self.controls.next_page(self.page))

    def collect_scope(self, task, scope, business_date):
        """Yield validated pages and reject duplicate, missing or changing rankings."""
        self.open_session()
        if self.needs_reset:
            self._initialize(task, business_date, restore=True)
            self.needs_reset = False
        # 分类状态独立，分类内总数冻结，更新时间不参与一致性校验。
        entries = []
        total = None
        target_pages = 1
        page_no = 1
        try:
            while page_no <= target_pages:
                # 每页只尝试一次；失败分类由批次在完整遍历后从第一页补采。
                try:
                    payload, params = self._capture_page(task, scope, business_date, page_no)
                except PlaywrightTimeoutError as error:
                    raise HttpRequestError("Taobao page timed out", category="page_response_timeout") from error
                # 原始接口字段、页排名和跨页完整性仍使用已测试的淘宝解析器。
                contract = validate_page_payload(payload, requested_page=page_no, expected_total=total)
                total, target_pages = contract.api_total, contract.target_page_count
                captured_at = datetime.now(TIMEZONE)
                page_entries = tuple(parse_page_entries(payload, page_no=page_no, captured_at=captured_at))
                self._check(business_date)
                entries.extend(page_entries)
                yield PageCapture(page_no, total, target_pages, captured_at, page_entries, payload, params)
                page_no += 1
                if page_no <= target_pages:
                    self._pump(random.uniform(self.settings.request_interval_seconds.min,
                                              self.settings.request_interval_seconds.max), business_date)
            validate_complete_ranking(entries, api_total=total)
        except (HttpRequestError, HttpResponseError, BrowserOperationError, ResponseContractError):
            # 下一分类或补采前重新初始化页面，不在失败页即时重试。
            self.initialized = False
            self.needs_reset = True
            raise

    def close(self) -> None:
        """Remove response listeners and close even a partially initialized session."""
        try:
            if self.capture is not None:
                self.capture.detach()
        finally:
            try:
                if self.session is not None:
                    self.session.close()
            finally:
                self.session = None
                self.page = None
                self.capture = None
                self.initialized = False
                self.needs_reset = False
