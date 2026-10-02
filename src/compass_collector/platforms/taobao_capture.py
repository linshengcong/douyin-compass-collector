"""Associate finished page responses with one explicit Taobao UI action."""

import json
from urllib.parse import parse_qs, urlsplit

from compass_collector.errors import HttpResponseError, ResponseContractError
from compass_collector.platforms.taobao_categories import CATEGORY_TREE_ENDPOINT_PATH
from compass_collector.platforms.taobao_product_rank import matches_rank_request


def matches_category_request(request) -> bool:
    """Accept only the observed HTTPS free taxonomy, without saving its URL."""
    try:
        # 分类监听也限制来源、端口和版本，拒绝重复业务参数。
        parts = urlsplit(request.url)
        return (parts.scheme == "https" and parts.hostname == "sycm.taobao.com"
                and parts.port in (None, 443) and parts.username is None
                and parts.password is None and parts.path == CATEGORY_TREE_ENDPOINT_PATH
                and parse_qs(parts.query).get("marketVersion") == ["free"])
    except (AttributeError, ValueError):
        return False


def read_completed_payload(response) -> dict:
    """Read a finished response outside callbacks, retaining failures locally."""
    if response.status != 200:
        raise HttpResponseError("Taobao HTTP response failed", category="ranking_http_error",
                                status_code=response.status)
    try:
        # 只在 requestfinished 之后由采集循环读取，不重入 Playwright 事件回调。
        payload = response.json()
    except ValueError as error:
        raise HttpResponseError("Taobao response is not JSON", category="invalid_json") from error
    if not isinstance(payload, dict):
        raise ResponseContractError("Taobao response is not an object", category="invalid_contract")
    if type(payload.get("code")) is not int or payload["code"] != 0:
        # 未验证的错误码不能借用抖音认证语义；正文仅供已有本地失败材料留档。
        raise ResponseContractError("Taobao business response failed", category="taobao_business_error",
                                    response_body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return payload


class TaobaoResponseCapture:
    """Cache objects only; the UI owner controls action boundaries and DOM checks."""

    def __init__(self, page):
        """Restrict response ownership to the task's one ranking page."""
        # 监听虽然安装在 context，其他标签页的同接口请求仍不能混入本任务。
        self.page = page
        # 每次 arm 递增代次，旧 requestfinished 无权进入新动作的队列。
        self.generation = 0
        self.expected = None
        self.requests = {}
        self.responses = {}
        self.pending = []
        # 分类发现只保留一个最新已完成响应，不缓存无限自动请求。
        self.category_response = None
        self.category_requests = set()
        # 同一动作中多个精确匹配请求无法证明哪个由点击产生，直接拒绝歧义。
        self.match_count = 0
        # context 生命周期由正式适配器持有，监听器可成组安装及清理。
        self.context = None
        self.listeners = (("request", self.on_request), ("response", self.on_response),
                          ("requestfinished", self.on_finished), ("requestfailed", self.on_failed))

    def attach(self, context) -> None:
        """Install all listeners before navigation, avoiding duplicate callbacks."""
        if self.context is context:
            return
        if self.context is not None:
            raise ValueError("Taobao capture already attached to another context")
        self.context = context
        try:
            for event, callback in self.listeners:
                context.on(event, callback)
        except Exception:
            self.detach()
            raise

    def detach(self) -> None:
        """Remove listeners and local evidence when a task session closes."""
        if self.context is not None:
            for event, callback in self.listeners:
                self.context.remove_listener(event, callback)
            self.context = None
        self.disarm()
        self.responses.clear()
        self.category_requests.clear()
        self.category_response = None

    def arm(self, expected: dict) -> int:
        """Start a fresh UI action before the caller touches the page."""
        self.disarm()
        self.generation += 1
        # 复制白名单，调用方后续修改参数不能改变已注册动作。
        self.expected = dict(expected)
        self.match_count = 0
        return self.generation

    def disarm(self) -> None:
        """Drop every pending rank object when actions end or recovery starts."""
        self.expected = None
        self.requests.clear()
        # 分类响应可独立完成；不能因为分页切换丢弃分类发现证据。
        self.responses = {request: response for request, response in self.responses.items()
                          if request in self.category_requests}
        self.pending.clear()

    def _owns(self, request) -> bool:
        """Reject requests lacking a frame or belonging to another page."""
        try:
            return request.frame.page is self.page
        except Exception:
            return False

    def on_request(self, request) -> None:
        """Register request identity at creation, never retroactively on response."""
        if not self._owns(request):
            return
        if matches_category_request(request):
            self.category_requests.add(request)
        if self.expected is not None and matches_rank_request(request, self.expected):
            self.match_count += 1
            self.requests[request] = self.generation

    def on_response(self, response) -> None:
        """Remember a response object without trying to read its unfinished body."""
        # response.request 的对象身份是唯一归属依据，不能改为 URL 或页码比较。
        request = response.request
        if request in self.requests or request in self.category_requests:
            self.responses[request] = response

    def on_finished(self, request) -> None:
        """Deliver only completed bodies from the still-active action generation."""
        # 失败或旧代次请求已被移除，不能被晚到响应重新注册。
        response = self.responses.pop(request, None)
        generation = self.requests.pop(request, None)
        if request in self.category_requests:
            self.category_requests.discard(request)
            if response is not None:
                self.category_response = response
        if response is not None and generation == self.generation and self.expected is not None:
            self.pending.append(response)

    def on_failed(self, request) -> None:
        """Release aborted requests; callers retain their bounded timeout policy."""
        self.requests.pop(request, None)
        self.responses.pop(request, None)
        self.category_requests.discard(request)

    def ensure_action(self, generation: int) -> None:
        """Check ownership again after a DOM wait pumps additional page events."""
        if generation != self.generation or self.expected is None:
            raise ResponseContractError("Taobao action generation changed", category="response_action_mismatch")
        if self.match_count > 1:
            raise ResponseContractError("Taobao action has ambiguous requests", category="ambiguous_page_response")

    def take(self, generation: int):
        """Return one finished response only after the caller confirms DOM state."""
        self.ensure_action(generation)
        return self.pending.pop(0) if self.pending else None
