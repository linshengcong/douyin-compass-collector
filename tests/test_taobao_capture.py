"""Synthetic event sequencing, never account traffic or real browser acceptance."""

from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from compass_collector.errors import HttpResponseError, ResponseContractError
from compass_collector.platforms.taobao_capture import (
    TaobaoResponseCapture, matches_category_request, read_completed_payload,
)
from compass_collector.platforms.taobao_product_rank import RANK_PATH

# 不含认证值，页参数只用于可观察的事件归属用例。
PARAMS = {"page": 1, "pageSize": 20, "dateType": "today", "cateId": "50021853"}


class Request:
    """Identity-hashable request with an explicit source page."""

    def __init__(self, page, params=None, url=None):
        """Do not emulate a URL-keyed request cache."""
        # 两个请求即使 URL 完全相同也保持不同对象身份。
        self.frame = SimpleNamespace(page=page)
        self.url = url or "https://sycm.taobao.com" + RANK_PATH + "?" + urlencode(params or PARAMS)


class Response:
    """Fail immediately if an event callback tries to read the response body."""

    def __init__(self, request, payload=None, status=200):
        """Keep body accessibility independently controllable from response headers."""
        # headers 已到达不代表下载结束，测试据此检测提前读取。
        self.request = request
        self.status = status
        self.payload = payload if payload is not None else {"code": 0, "data": {}}
        self.finished = False
        self.reads = 0

    def json(self):
        """Require requestfinished before permitting a body read."""
        assert self.finished
        self.reads += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def complete(capture, response):
    """Deliver an actual finish event after headers, with no implicit request."""
    capture.on_response(response)
    response.finished = True
    capture.on_finished(response.request)


def test_body_is_not_read_in_callbacks_and_finish_is_required():
    """Response headers alone must never make an entry ready for collection."""
    # 动作先注册，再创建页面请求。
    page = object()
    capture = TaobaoResponseCapture(page)
    generation = capture.arm(PARAMS)
    request = Request(page)
    response = Response(request)
    capture.on_request(request)
    capture.on_response(response)
    assert capture.take(generation) is None
    assert response.reads == 0
    response.finished = True
    capture.on_finished(request)
    assert capture.take(generation) is response
    assert read_completed_payload(response)["code"] == 0
    assert response.reads == 1


def test_old_and_unarmed_requests_cannot_enter_a_new_action():
    """A late requestfinished must not be assigned by its matching URL."""
    # 旧动作已收到头部但尚未完成，开始新动作时必须失效。
    page = object()
    capture = TaobaoResponseCapture(page)
    old_generation = capture.arm(PARAMS)
    old = Response(Request(page))
    capture.on_request(old.request)
    capture.on_response(old)
    generation = capture.arm(PARAMS)
    complete(capture, old)
    assert capture.take(generation) is None
    with pytest.raises(ResponseContractError, match="generation"):
        capture.take(old_generation)
    # 已在点击前创建的自动刷新，即便响应在点击后到达也不能注册。
    capture.disarm()
    unarmed = Response(Request(page))
    capture.on_request(unarmed.request)
    generation = capture.arm(PARAMS)
    complete(capture, unarmed)
    assert capture.take(generation) is None


@pytest.mark.parametrize("change", ["other_page", "wrong_page", "wrong_size", "duplicate_query", "credentials", "wrong_origin"])
def test_unrelated_requests_are_rejected(change):
    """Page, origin and entire business query all constrain request ownership."""
    # 每项破坏一个边界，不能只验证 URL 子串是否存在。
    page = object()
    capture = TaobaoResponseCapture(page)
    generation = capture.arm(PARAMS)
    request = Request(page)
    if change == "other_page":
        request.frame.page = object()
    elif change == "wrong_page":
        request = Request(page, {**PARAMS, "page": 2})
    elif change == "wrong_size":
        request = Request(page, {**PARAMS, "pageSize": 10})
    elif change == "duplicate_query":
        request.url += "&page=1"
    elif change == "credentials":
        request.url = request.url.replace("https://", "https://synthetic:synthetic@")
    else:
        request.url = request.url.replace("sycm.taobao.com", "example.invalid")
    capture.on_request(request)
    complete(capture, Response(request))
    assert capture.take(generation) is None


def test_multiple_matching_requests_fail_as_ambiguous():
    """Two same-query requests cannot silently select an automatic refresh."""
    # 即使只有其中一个先完成，歧义也应阻止采集。
    page = object()
    capture = TaobaoResponseCapture(page)
    generation = capture.arm(PARAMS)
    first, second = Request(page), Request(page)
    capture.on_request(first)
    capture.on_request(second)
    complete(capture, Response(second))
    with pytest.raises(ResponseContractError) as failure:
        capture.take(generation)
    assert failure.value.category == "ambiguous_page_response"


def test_failed_request_is_removed_and_new_action_can_retry():
    """Recovery clears identity caches and accepts a newly created request."""
    # 恢复必须启动新代次，而非把旧失败请求重放为成功。
    page = object()
    capture = TaobaoResponseCapture(page)
    capture.arm(PARAMS)
    failed = Request(page)
    capture.on_request(failed)
    capture.on_failed(failed)
    generation = capture.arm(PARAMS)
    response = Response(Request(page))
    capture.on_request(response.request)
    complete(capture, response)
    assert capture.take(generation) is response
    assert not capture.requests and not capture.responses


def test_taxonomy_finish_survives_rank_action_changes():
    """Category discovery is independent of ranking generations and source-safe."""
    # 分类请求在导航前安装监听后捕获，只存最新完整响应。
    page = object()
    capture = TaobaoResponseCapture(page)
    request = Request(page, url="https://sycm.taobao.com/mc/common/free/getCateInfo.json?marketVersion=free")
    capture.on_request(request)
    capture.arm(PARAMS)
    response = Response(request)
    complete(capture, response)
    assert capture.category_response is response
    assert not matches_category_request(Request(page, url=request.url + "&marketVersion=free"))


def test_listener_lifecycle_does_not_duplicate_or_retain_closed_session():
    """One task owns four listeners; closing removes all pending source objects."""
    # 简单事件发射器模拟 context 安装，不启动浏览器。
    from pyee import EventEmitter
    context = EventEmitter()
    capture = TaobaoResponseCapture(object())
    capture.attach(context)
    capture.attach(context)
    for event, callback in capture.listeners:
        assert context.listeners(event) == [callback]
    capture.arm(PARAMS)
    capture.detach()
    capture.detach()
    assert capture.context is None and capture.expected is None
    assert capture.category_response is None
    for event, _ in capture.listeners:
        assert context.listeners(event) == []


@pytest.mark.parametrize("payload,category", [(ValueError("synthetic"), "invalid_json"),
                                            ([], "invalid_contract"),
                                            ({"code": 12345, "message": "synthetic failure"}, "taobao_business_error")])
def test_unknown_response_failures_keep_safe_error_categories(payload, category):
    """Unknown Taobao codes must not become invented Compass authentication codes."""
    # 响应正文仅附着在本地错误对象，异常文本不包含业务正文。
    response = Response(Request(object()), payload)
    response.finished = True
    with pytest.raises((HttpResponseError, ResponseContractError)) as failure:
        read_completed_payload(response)
    assert failure.value.category == category
    assert "synthetic" not in str(failure.value)
    if category == "taobao_business_error":
        assert b"12345" in failure.value.response_body
