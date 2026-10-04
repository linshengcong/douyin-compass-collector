"""Controlled page events prove orchestration, not real Taobao selector acceptance."""

from datetime import timedelta
from time import sleep
from types import SimpleNamespace

from pyee import EventEmitter
import pytest

from compass_collector.config import CollectionConfig, TaskConfig
from compass_collector.errors import AuthRequiredError, BrowserOperationError, CollectionInterruptedError, HttpRequestError, ResponseContractError
from compass_collector.models import DiscoveredScope
from compass_collector.platforms import taobao
from compass_collector.platforms.taobao_product_rank import build_expected_params
from test_taobao_capture import Request, Response
from test_taobao_product_rank import CAPTURED_AT, page_payload
from test_taobao_categories import TREE


class Controls:
    """Emit synthetic requests only when the adapter invokes visible actions."""

    def __init__(self, context, task):
        """Track initialization count and one optional page failure."""
        # 这些计数用于检验分页和恢复，避免按实现返回固定 PageCapture。
        self.context = context
        self.task = task
        self.initializations = 0
        self.enters = 0
        self.page_no = 1
        self.scope = None
        self.fail_next = False
        self.total = 41

    def enter(self, page):
        """Produce the taxonomy after listeners have actually been attached."""
        self.enters += 1
        self.page_no = 1
        # 分类响应使用现有明确标注的合成平铺树。
        request = Request(page, url="https://sycm.taobao.com/mc/common/free/getCateInfo.json?marketVersion=free")
        self.emit(Response(request, TREE))

    def authenticated(self, page):
        """Keep test transport independent of real account login state."""
        return True

    def resume_after_login(self, page):
        """Synthetic pages have no merchant-home redirect unless a test supplies it."""
        return False

    def authentication_pending(self, page):
        """Only explicit delayed-render tests simulate an unfinished rank shell."""
        return False

    def initialize(self, page, task, business_date):
        """Count twenty-row initialization, including recovery rebuilds only."""
        self.initializations += 1

    def select_scope(self, page, scope):
        """A category action resets page one without touching page size."""
        self.scope = scope
        self.page_no = 1
        self.rank(page)

    def next_page(self, page):
        """Fail once before a response so restoration must restart from page one."""
        if self.fail_next:
            self.fail_next = False
            raise HttpRequestError("Synthetic next page failed", category="page_response_timeout")
        self.page_no += 1
        self.rank(page)

    def confirm_page(self, page, page_no, total):
        """Require the actual action position to equal the captured query page."""
        assert self.page_no == page_no
        assert total == self.total

    def rank(self, page):
        """Exercise the production query matcher and parser using distinct events."""
        # query 不含签名；不同页更新时间故意不同。
        params = build_expected_params(self.task, self.scope, CAPTURED_AT.date(), self.page_no)
        self.emit(Response(Request(page, params), page_payload(self.page_no, total=self.total,
                                                             update_time=f"synthetic-{self.page_no}")))

    def emit(self, response):
        """Keep request, headers and finished callbacks in their real order."""
        self.context.emit("request", response.request)
        self.context.emit("response", response)
        response.finished = True
        self.context.emit("requestfinished", response.request)


def setup(monkeypatch):
    """Inject browser resources and clock without contacting the Taobao origin."""
    # 固定北京时间可真实执行跨午夜检查，测试不依赖当天日期。
    monkeypatch.setattr(taobao, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    context = EventEmitter()
    page = SimpleNamespace(wait_for_timeout=lambda milliseconds: sleep(milliseconds / 1000))
    closed = []
    session = SimpleNamespace(context=context, page=page, close=lambda: closed.append(True))
    monkeypatch.setattr(taobao, "open_browser", lambda config: session)
    task = TaskConfig(id="taobao_test", platform="taobao", display_name="合成测试", schedule="0 14 * * *")
    controls = Controls(context, task)
    adapter = taobao.TaobaoAdapter(None, CollectionConfig(network_retry_attempts=1,
                                                        request_interval_seconds={"min": 0.01, "max": 0.01}),
                                    manual=False, controls=controls)
    # 恢复退避不改变事件序列，测试只缩短等待而不伪造响应结果。
    monkeypatch.setattr(adapter, "_pump", lambda seconds, business_date=None: adapter._check(business_date))
    scope = DiscoveredScope(1, "50021853", ("合成一级", "合成二级", "合成三级"),
                            {"cate_id": "50021853", "cate_flag": "0"})
    return adapter, controls, task, scope, closed


def test_complete_pages_and_multiple_scopes_initialize_only_once(monkeypatch):
    """The production adapter yields twenty/twenty/one and preserves page size."""
    # 第二分类重复商品允许存在，但每个分类内部必须连续完整。
    adapter, controls, task, scope, closed = setup(monkeypatch)
    discovery = adapter.discover_scopes(task)
    assert discovery.payload == TREE
    for _ in range(2):
        pages = list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
        assert [len(page.entries) for page in pages] == [20, 20, 1]
        assert [page.page_no for page in pages] == [1, 2, 3]
        assert [entry.rank for page in pages for entry in page.entries] == list(range(1, 42))
        assert all(page.safe_params["pageSize"] == 20 for page in pages)
    assert controls.initializations == 1 and controls.enters == 1
    adapter.close()
    assert closed == [True]
    assert not controls.context.listeners("request")


def test_manual_home_landing_returns_to_ranking_once_before_authentication(monkeypatch):
    """Manual login ending on the merchant home must not spend the whole auth timeout there."""
    # 登录完成后首页尚无分页；只有回到榜单才能取得真实认证页面判断。
    adapter, controls, _, _, _ = setup(monkeypatch)
    adapter.manual = True
    # 缺少衔接时快速失败，不让回归测试沿用三分钟人工等待预算。
    adapter.settings.manual_auth_wait_seconds = 0.1
    resumed = []

    def resume(page):
        """Record the single transition from a logged-in home to the rank page."""
        resumed.append(True)
        controls.enter(page)
        return True

    monkeypatch.setattr(controls, "resume_after_login", resume)
    monkeypatch.setattr(controls, "authenticated", lambda page: controls.enters >= 2)
    adapter.open_session()
    assert resumed == [True]
    assert controls.enters == 2
    adapter.close()


def test_unattended_rank_shell_waits_for_paging_before_requiring_login(monkeypatch):
    """A valid session whose rank controls render late is not an authentication failure."""
    # 模拟 DOMContentLoaded 后一轮事件泵才显示分页，不允许调度误报 auth_required。
    adapter, controls, _, _, _ = setup(monkeypatch)
    rendered = []
    monkeypatch.setattr(controls, "authenticated", lambda page: bool(rendered))
    monkeypatch.setattr(controls, "authentication_pending", lambda page: True)
    monkeypatch.setattr(adapter, "_pump", lambda *args: rendered.append(True))
    adapter.open_session()
    assert rendered == [True]
    adapter.close()


@pytest.mark.parametrize("request_started", [True, False])
def test_category_click_timeout_uses_completed_response_without_retry(monkeypatch, request_started):
    """A click may time out after committing; completed request identity decides acceptance."""
    # 复现真实taobao_category_select点击超时，区别已产生请求和根本未触发请求。
    adapter, controls, task, scope, _ = setup(monkeypatch)
    # 无响应分支使用短预算；仍经过生产截止时间判断，不伪造超时结论。
    adapter.settings.response_timeout_seconds = 0.01
    original_select = controls.select_scope
    attempts = []
    error = BrowserOperationError("Category click timed out", category="browser_page_error",
                                  failed_step="taobao_category_select", exception_type="TimeoutError", screenshot=b"local")

    def select_then_timeout(page, selected_scope):
        """Optionally complete the real synthetic event chain before reporting timeout."""
        attempts.append(True)
        if request_started:
            original_select(page, selected_scope)
        raise error

    monkeypatch.setattr(controls, "select_scope", select_then_timeout)
    if request_started:
        pages = list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
        assert [page.page_no for page in pages] == [1, 2, 3]
        assert len(attempts) == 1 and controls.enters == controls.initializations == 1
    else:
        with pytest.raises(BrowserOperationError) as failure:
            list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
        assert failure.value is error and len(attempts) == 1
        assert controls.enters == controls.initializations == 1
    adapter.close()


def test_timeout_ends_attempt_and_next_collection_starts_at_page_one(monkeypatch):
    """页内失败立即退出，下一次分类采集允许使用新的实时总数。"""
    # 模拟第二页失败后，由分类编排重新调用适配器。
    adapter, controls, task, scope, _ = setup(monkeypatch)
    iterator = adapter.collect_scope(task, scope, CAPTURED_AT.date())
    first = next(iterator)
    controls.fail_next = True
    with pytest.raises(HttpRequestError):
        list(iterator)
    assert first.page_no == 1
    assert controls.enters == controls.initializations == 1
    controls.total = 2
    # 补采必须重新从第一页开始，不能回放旧页或复用旧总数。
    pages = list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
    assert [page.page_no for page in pages] == [1]
    assert len(pages[0].entries) == 2 and pages[0].api_total == 2
    assert controls.enters == controls.initializations == 2
    adapter.close()


@pytest.mark.parametrize("step", ["taobao_next_page", "unrecognized_step"])
def test_control_failure_is_attempted_once_with_original_diagnostics(monkeypatch, step):
    """即使配置页重试，淘宝也只点击一次并保留原始故障步骤与截图。"""
    # 与罗盘共用的 network_retry_attempts 不再影响淘宝。
    adapter, controls, task, scope, _ = setup(monkeypatch)
    iterator = adapter.collect_scope(task, scope, CAPTURED_AT.date())
    next(iterator)
    # 点击计数直接检查是否仍有隐藏的页内重试。
    calls = []
    error = BrowserOperationError("Control unavailable", category="browser_page_error",
        failed_step=step, exception_type="RuntimeError", screenshot=b"local")

    def unavailable(page):
        """记录失败点击，并返回真实浏览器错误类型。"""
        calls.append(True)
        raise error

    monkeypatch.setattr(controls, "next_page", unavailable)
    with pytest.raises(BrowserOperationError) as failure:
        list(iterator)
    assert failure.value is error and failure.value.screenshot == b"local"
    assert len(calls) == 1
    assert controls.enters == controls.initializations == 1
    adapter.close()


@pytest.mark.parametrize("step", ["taobao_page_size_lost", "taobao_active_page"])
def test_completed_sixth_response_is_rejected_without_page_replay(monkeypatch, step):
    """已保存五页后页面状态丢失，不能接受第六页或立即回放第一页。"""
    # 真实事件捕获仍经过生产响应校验，故障发生在响应完成后的 DOM 确认。
    adapter, controls, task, scope, _ = setup(monkeypatch)
    controls.total = 121
    iterator = adapter.collect_scope(task, scope, CAPTURED_AT.date())
    saved = [next(iterator) for _ in range(5)]
    original_confirm = controls.confirm_page
    failures = []
    error = BrowserOperationError("Active page lost", category="browser_page_error",
                                  failed_step=step, exception_type="RuntimeError", screenshot=b"local-sixth-page")

    def reject_sixth(page, page_no, total):
        """仅在第六页完整响应后失去可见状态。"""
        original_confirm(page, page_no, total)
        if page_no == 6:
            failures.append(True)
            raise error

    monkeypatch.setattr(controls, "confirm_page", reject_sixth)
    with pytest.raises(BrowserOperationError) as caught:
        list(iterator)
    assert caught.value is error
    assert [page.page_no for page in saved] == [1, 2, 3, 4, 5]
    assert len(failures) == 1 and controls.enters == controls.initializations == 1
    adapter.close()


def test_wrong_business_date_stops_before_initialization(monkeypatch):
    """Cross-midnight work must not initialize current data into yesterday's batch."""
    adapter, controls, task, scope, _ = setup(monkeypatch)
    with pytest.raises(CollectionInterruptedError) as failure:
        list(adapter.collect_scope(task, scope, CAPTURED_AT.date() - timedelta(days=1)))
    assert failure.value.category == "business_date_changed"
    assert controls.initializations == 0
    adapter.close()


def test_refresh_during_dom_confirmation_cannot_escape_ambiguity_check(monkeypatch):
    """A later same-query request must invalidate the captured action too."""
    adapter, controls, task, scope, _ = setup(monkeypatch)
    # confirm_page 的实际 DOM 等待可能收到新事件，必须在返回数据前再次检查。
    monkeypatch.setattr(controls, "confirm_page", lambda page, page_no, total: controls.rank(page))
    with pytest.raises(ResponseContractError) as failure:
        list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
    assert failure.value.category == "ambiguous_page_response"
    adapter.close()


def test_manual_auth_expiry_ends_attempt_without_page_retry(monkeypatch):
    """手动模式启动时仍可登录，但采集中失效按任务中断处理。"""
    # 登录失效不能被普通分类补采吞掉或反复恢复当前页。
    adapter, controls, task, scope, _ = setup(monkeypatch)
    adapter.manual = True
    controls.total = 1
    answers = iter([True, False])
    monkeypatch.setattr(controls, "authenticated", lambda page: next(answers, False))
    with pytest.raises(AuthRequiredError):
        list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
    assert controls.initializations == controls.enters == 1
    adapter.close()


def test_scheduler_auth_expiry_never_waits_for_manual_recovery(monkeypatch):
    """Unattended mode terminates immediately after a page login failure."""
    adapter, controls, task, scope, _ = setup(monkeypatch)
    # 启动可用，但本次动作鉴权失效，不能隐式变成人工等待。
    answers = iter([True, False])
    monkeypatch.setattr(controls, "authenticated", lambda page: next(answers, False))
    with pytest.raises(AuthRequiredError) as failure:
        list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
    assert failure.value.category == "auth_required"
    assert controls.enters == 1
    adapter.close()
