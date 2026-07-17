"""Shared Compass HTTP transport, throttling, and Cookie-scope tests."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import compass_collector.http_client as http_client_module
from compass_collector.browser import (
    COMPASS_API_COOKIE_SCOPES,
    SAFE_RANKING_PAGE_URL,
    BrowserSession,
)
from compass_collector.category_discovery import build_category_request_params
from compass_collector.config import HttpConfig, IntervalConfig, load_config
from compass_collector.errors import HttpResponseError
from compass_collector.http_client import CompassHttpClient


# 动态、追踪和签名参数不得进入分类树请求。
FORBIDDEN_DYNAMIC_PARAMS = {
    "_lid",
    "verifyFp",
    "fp",
    "msToken",
    "a_bogus",
}


def build_http_config() -> HttpConfig:
    """Build the fixed serial HTTP settings used by isolated client tests."""

    # 测试配置使用当前 0.1～0.3 秒统一间隔。
    interval_config = IntervalConfig(min=0.1, max=0.3)
    return HttpConfig(
        concurrency=1,
        request_interval_seconds=interval_config,
        connect_timeout_seconds=10,
        read_timeout_seconds=30,
    )


def patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Route the production client through an in-memory MockTransport."""

    # 在 monkeypatch 前保留真实 Client 类，避免工厂函数递归调用自己。
    real_client_class = httpx.Client
    # MockTransport 只在进程内观察请求，不访问真实网络。
    transport = httpx.MockTransport(handler)

    def build_mock_client(**kwargs: Any) -> httpx.Client:
        """Preserve production client options while injecting the mock transport."""

        # 生产代码仍决定 headers、timeout 和 redirect 策略。
        return real_client_class(transport=transport, **kwargs)

    monkeypatch.setattr(http_client_module.httpx, "Client", build_mock_client)


def test_category_request_uses_only_fixed_endpoint_and_three_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send the category request without browser tracing or signature parameters."""

    # 捕获请求用于核对最小传输契约。
    captured_requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        """Capture one category request and return a valid JSON object."""

        captured_requests.append(request)
        return httpx.Response(200, json={"st": 0, "data": {}})

    patch_httpx_client(monkeypatch, handle_request)
    # 首次请求的 waiter 不应被调用。
    observed_delays: list[float] = []

    def wait_for_delay(delay_seconds: float) -> bool:
        """Record an unexpected first-request delay without sleeping."""

        observed_delays.append(delay_seconds)
        return False

    # 客户端不注入任何真实 Cookie。
    client = CompassHttpClient(
        build_http_config(),
        cookies=[],
        user_agent="CompassCollectorTest/1.0",
        wait_for_delay=wait_for_delay,
    )
    try:
        # 分类参数由业务契约函数统一构造。
        response = client.get_category_tree(build_category_request_params())
    finally:
        client.close()

    assert response.status_code == 200
    assert observed_delays == []
    assert len(captured_requests) == 1
    # 唯一请求必须命中已确认的分类树路径。
    request = captured_requests[0]
    assert request.url.path == "/compass_api/config_center/category/cate_list"
    assert dict(request.url.params) == {
        "level": "4",
        "scene": "9",
        "default_cate_to_level": "2",
    }
    assert FORBIDDEN_DYNAMIC_PARAMS.isdisjoint(request.url.params.keys())
    assert request.headers["referer"] == SAFE_RANKING_PAGE_URL


def test_same_client_waits_before_every_request_after_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply the shared 0.1-0.3 second throttle across repeated API calls."""

    # 请求计数用于确认客户端没有隐式重试。
    request_count = 0
    # 路径序列用于证明分类树与榜单分页共用同一节流边界。
    requested_paths: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        """Return one successful JSON response for each explicit call."""

        nonlocal request_count
        # 每次 handler 调用都对应一次显式网络尝试。
        request_count += 1
        requested_paths.append(request.url.path)
        return httpx.Response(200, json={"st": 0, "data": {}})

    patch_httpx_client(monkeypatch, handle_request)
    # fake waiter 避免测试真实等待。
    observed_delays: list[float] = []

    def wait_for_delay(delay_seconds: float) -> bool:
        """Record each generated delay and continue collection."""

        observed_delays.append(delay_seconds)
        return False

    # 同一客户端代表同一顶层任务的全部罗盘 API 请求。
    client = CompassHttpClient(
        build_http_config(),
        cookies=[],
        user_agent="CompassCollectorTest/1.0",
        wait_for_delay=wait_for_delay,
    )
    # 真实任务只用来提供已验证的榜单 endpoint_path。
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    try:
        # 分类树立即发送，后续榜单分页各等待一次。
        client.get_category_tree(build_category_request_params())
        client.get_product_rank_page(task, {"page_no": 1})
        client.get_product_rank_page(task, {"page_no": 2})
    finally:
        client.close()

    assert request_count == 3
    assert requested_paths == [
        "/compass_api/config_center/category/cate_list",
        "/compass_api/shop/product/product_rank/market_hot_sale",
        "/compass_api/shop/product/product_rank/market_hot_sale",
    ]
    assert len(observed_delays) == 2
    assert all(0.1 <= delay_seconds <= 0.3 for delay_seconds in observed_delays)


def test_failed_request_still_forces_delay_before_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the shared throttle active when the previous response failed."""

    # 响应序列模拟一次服务端失败后由上层继续其他分类。
    response_statuses = iter((500, 200))
    # 处理计数证明 client 本身没有重试。
    request_count = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        """Return the next planned HTTP status for each explicit request."""

        nonlocal request_count
        request_count += 1
        # 每个响应都保留 JSON body，失败分类只由状态码决定。
        status_code = next(response_statuses)
        return httpx.Response(status_code, json={"st": 0, "data": {}})

    patch_httpx_client(monkeypatch, handle_request)
    # 间隔只记录不真实 sleep。
    observed_delays: list[float] = []

    def wait_for_delay(delay_seconds: float) -> bool:
        """Record the post-failure delay and continue collection."""

        observed_delays.append(delay_seconds)
        return False

    # 客户端在首次失败后保留“已请求”状态。
    client = CompassHttpClient(
        build_http_config(),
        cookies=[],
        user_agent="CompassCollectorTest/1.0",
        wait_for_delay=wait_for_delay,
    )
    try:
        with pytest.raises(HttpResponseError) as captured_error:
            client.get_category_tree(build_category_request_params())
        # 上层决定继续后，下一次请求仍必须先经过统一间隔。
        response = client.get_category_tree(build_category_request_params())
    finally:
        client.close()

    assert captured_error.value.category == "http_error"
    assert response.status_code == 200
    assert request_count == 2
    assert len(observed_delays) == 1
    assert 0.1 <= observed_delays[0] <= 0.3


def test_cookie_scope_covers_category_and_ranking_endpoints() -> None:
    """Query Playwright with both API paths before applying the name allowlist."""

    class FakeBrowserContext:
        """Capture Cookie scope URLs and return mixed allowlisted candidates."""

        def __init__(self) -> None:
            """Initialize the captured scope list."""

            # None 表示 BrowserSession 尚未读取 Cookie。
            self.requested_scopes: list[str] | None = None

        def cookies(self, scopes: list[str]) -> list[dict[str, Any]]:
            """Return one allowed and one disallowed Cookie candidate."""

            self.requested_scopes = scopes
            return [
                {
                    "name": "sessionid",
                    "value": "test-session-value",
                    "domain": ".jinritemai.com",
                    "path": "/",
                },
                {
                    "name": "not_allowlisted",
                    "value": "must-not-pass",
                    "domain": ".jinritemai.com",
                    "path": "/",
                },
            ]

    # 假上下文不启动 Chrome，只验证 Cookie 查询边界。
    fake_context = FakeBrowserContext()
    # playwright/page 在成功 Cookie 路径中不会被访问。
    session = BrowserSession(
        playwright=object(),  # type: ignore[arg-type]
        context=fake_context,  # type: ignore[arg-type]
        page=object(),  # type: ignore[arg-type]
    )

    # 白名单仅允许 sessionid 进入运行时 HTTP Cookie jar。
    cookies = session.whitelisted_cookies(["sessionid"])

    assert fake_context.requested_scopes == list(COMPASS_API_COOKIE_SCOPES)
    assert COMPASS_API_COOKIE_SCOPES == (
        "https://compass.jinritemai.com/compass_api/config_center/category/cate_list",
        "https://compass.jinritemai.com/compass_api/shop/product/product_rank/market_hot_sale",
    )
    assert [cookie["name"] for cookie in cookies] == ["sessionid"]
