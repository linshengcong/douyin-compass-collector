"""Minimal synchronous HTTP client shared by all Compass API requests."""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Any

import httpx

from compass_collector.browser import SAFE_RANKING_PAGE_URL
from compass_collector.category_discovery import CATEGORY_TREE_ENDPOINT_PATH
from compass_collector.config import HttpConfig, TaskConfig
from compass_collector.errors import (
    AuthRequiredError,
    CollectionInterruptedError,
    HttpRequestError,
    HttpResponseError,
)


# 固定接口源不包含任何账号或签名参数。
COMPASS_API_ORIGIN = "https://compass.jinritemai.com"
# 标准重定向对预期 JSON 接口而言视为登录态异常。
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
# 等待函数返回 True 表示用户在间隔中请求中止。
DelayWaiter = Callable[[float], bool]
# 单分类分页预取只开放四个 worker，避免分类级生命周期被并发放大。
MAX_PAGE_FETCH_WORKERS = 4


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    """Keep parsed JSON and local-only response bytes together for diagnostics."""

    # payload 进入契约校验，body 只在失败时进入本地受限材料。
    payload: dict[str, Any]
    body: bytes
    status_code: int


def _sleep_without_interruption(delay_seconds: float) -> bool:
    """Sleep in terminal mode and report that no cooperative stop occurred."""

    time.sleep(delay_seconds)
    return False


class CompassHttpClient:
    """Call Compass APIs through one authenticated Cookie jar and shared throttle."""

    def __init__(
        self,
        config: HttpConfig,
        cookies: list[dict[str, Any]],
        user_agent: str,
        *,
        wait_for_delay: DelayWaiter | None = None,
    ) -> None:
        """Create one minimal httpx client and unified request throttle."""

        # 超时对象分别限制连接和响应读取阶段。
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )
        # 最小请求头不包含认证值或浏览器追踪参数。
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": SAFE_RANKING_PAGE_URL,
            "User-Agent": user_agent,
            "agw-js-conv": "str",
        }
        # HTTP 客户端不自动跟随可能指向登录页的重定向。
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )
        for cookie in cookies:
            # Cookie 的 domain/path 由 Playwright 提供，值不进入日志。
            self._client.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
        # 请求间隔边界同时覆盖分类、分页和分类切换。
        self._minimum_interval = config.request_interval_seconds.min
        self._maximum_interval = config.request_interval_seconds.max
        # GUI 传入可中断 waiter，终端模式默认使用 sleep。
        self._wait_for_delay = wait_for_delay or _sleep_without_interruption
        # 第一个罗盘 API 请求立即发送，后续请求才等待。
        self._has_requested = False
        # 请求启动锁让并发分页仍共享一个全局随机间隔。
        self._request_start_lock = Lock()
        # page_fetch_workers 是分类内分页预取的实际 worker 数。
        self.page_fetch_workers = min(config.page_concurrency, MAX_PAGE_FETCH_WORKERS)
        # 分页绝对进度上限覆盖连接、读取和同分类请求启动间隔，防止底层连接永久悬挂。
        self.page_fetch_wait_timeout_seconds = (
            config.connect_timeout_seconds
            + config.read_timeout_seconds
            + (self.page_fetch_workers * config.request_interval_seconds.max)
            + 5
        )
        # 全局请求名额与分页看门狗共用截止时间，避免失联请求耗尽名额后永久阻塞。
        self.request_slot_wait_timeout_seconds = self.page_fetch_wait_timeout_seconds
        # level1_fetch_workers 是一级分类组调度的实际 worker 数。
        self.level1_fetch_workers = config.level1_concurrency
        # 请求信号量跨一级分类和分页线程共享，限制真实在途 HTTP 请求数。
        self._in_flight_requests = BoundedSemaphore(config.max_in_flight_requests)

    def close(self) -> None:
        """Release connections and the in-memory Cookie jar."""

        self._client.close()

    def _wait_before_request(self) -> None:
        """Apply one random delay before every request after the first attempt."""

        # 并发分页只能并发等待响应，不能并发跳过请求启动间隔。
        with self._request_start_lock:
            if not self._has_requested:
                self._has_requested = True
                return
            # 间隔每次独立随机生成，不使用固定频率。
            delay_seconds = random.uniform(
                self._minimum_interval,
                self._maximum_interval,
            )
            if self._wait_for_delay(delay_seconds):
                raise CollectionInterruptedError(
                    "Collection interrupted during request interval",
                    category="interrupted",
                )

    def _get_json(
        self,
        endpoint_path: str,
        params: dict[str, str | int],
    ) -> HttpJsonResponse:
        """Request one fixed Compass endpoint without retries and parse its JSON."""

        # 取得名额后才进入统一启动间隔，等待响应期间持续占用名额。
        # 等待名额也必须有上限，后台失联请求不能让后续分类永久卡住。
        acquired_request_slot = self._in_flight_requests.acquire(
            timeout=self.request_slot_wait_timeout_seconds
        )
        if not acquired_request_slot:
            raise HttpRequestError(
                "Timed out waiting for an available Compass request slot",
                category="timeout",
            )
        try:
            self._wait_before_request()
            # 固定源与代码内部路径组成不含动态签名的接口地址。
            endpoint_url = f"{COMPASS_API_ORIGIN}{endpoint_path}"
            try:
                # 业务参数由 httpx 编码，请求 URL 不写入日志。
                response = self._client.get(endpoint_url, params=params)
            except httpx.TimeoutException as exc:
                raise HttpRequestError(
                    "HTTP request timed out",
                    category="timeout",
                ) from exc
            except httpx.RequestError as exc:
                raise HttpRequestError(
                    "HTTP request failed before receiving a response",
                    category="network_error",
                ) from exc
        finally:
            # 包括启动间隔中止和网络异常在内的所有路径都必须归还名额。
            self._in_flight_requests.release()
        # 响应 body 仅用于 JSON 解析或本地受限失败留档。
        response_body = response.content
        if response.status_code in {401, 403} | REDIRECT_STATUS_CODES:
            raise AuthRequiredError(
                "Compass authentication is required",
                category="auth_required",
                status_code=response.status_code,
                response_body=response_body,
            )
        if response.status_code == 429:
            raise HttpResponseError(
                "Compass returned a rate-limit response",
                category="rate_limited",
                status_code=response.status_code,
                response_body=response_body,
            )
        if not 200 <= response.status_code < 300:
            raise HttpResponseError(
                "Compass returned a non-success HTTP response",
                category="http_error",
                status_code=response.status_code,
                response_body=response_body,
            )
        try:
            # 解析后的响应必须是 JSON 对象才能进入契约校验。
            payload = response.json()
        except ValueError as exc:
            raise HttpResponseError(
                "Compass response is not valid JSON",
                category="invalid_json",
                status_code=response.status_code,
                response_body=response_body,
            ) from exc
        if not isinstance(payload, dict):
            raise HttpResponseError(
                "Compass JSON root is not an object",
                category="invalid_json_root",
                status_code=response.status_code,
                response_body=response_body,
            )
        return HttpJsonResponse(
            payload=payload,
            body=response_body,
            status_code=response.status_code,
        )

    def get_category_tree(
        self,
        params: dict[str, str | int],
    ) -> HttpJsonResponse:
        """Request the fixed category-tree endpoint once per top-level task."""

        return self._get_json(CATEGORY_TREE_ENDPOINT_PATH, params)

    def get_product_rank_page(
        self,
        task: TaskConfig,
        params: dict[str, str | int],
    ) -> HttpJsonResponse:
        """Request one product ranking page through the shared throttle."""

        return self._get_json(task.rank.endpoint_path, params)
