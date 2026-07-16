"""Minimal synchronous HTTP client for the verified Compass endpoint."""

from dataclasses import dataclass
from typing import Any

import httpx

from compass_collector.browser import SAFE_RANKING_PAGE_URL
from compass_collector.config import HttpConfig, TaskConfig
from compass_collector.errors import AuthRequiredError, HttpRequestError, HttpResponseError


# 固定接口源不包含任何账号或签名参数。
COMPASS_API_ORIGIN = "https://compass.jinritemai.com"
# 标准重定向状态对预期 JSON 接口而言视为登录态异常。
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


@dataclass(frozen=True, slots=True)
class HttpPageResponse:
    """Keep parsed JSON and local-only response bytes together for diagnostics."""

    payload: dict[str, Any]
    body: bytes
    status_code: int


class ProductRankHttpClient:
    """Call product ranking pages serially with runtime browser authentication."""

    def __init__(
        self,
        config: HttpConfig,
        cookies: list[dict[str, Any]],
        user_agent: str,
    ) -> None:
        """Create an httpx client with only the agreed minimal request surface."""

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

    def close(self) -> None:
        """Release connections and the in-memory Cookie jar."""

        self._client.close()

    def get_page(
        self,
        task: TaskConfig,
        params: dict[str, str | int],
    ) -> HttpPageResponse:
        """Request one page without retries and return its parsed JSON."""

        # 固定源和经验证路径组成不含动态签名的接口地址。
        endpoint_url = f"{COMPASS_API_ORIGIN}{task.rank.endpoint_path}"
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
        return HttpPageResponse(
            payload=payload,
            body=response_body,
            status_code=response.status_code,
        )
