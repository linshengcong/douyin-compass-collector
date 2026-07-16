"""Safe error types that never embed request URLs, headers, or Cookie values."""


class CollectorError(Exception):
    """Base error carrying only a safe category and optional response metadata."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        response_body: bytes | None = None,
    ) -> None:
        """Store diagnostic metadata without adding sensitive request context."""

        super().__init__(message)
        # 稳定错误分类用于 Manifest 和脱敏控制台输出。
        self.category = category
        # HTTP 状态码不包含认证信息，可用于诊断。
        self.status_code = status_code
        # 失败响应仅供受限本地留档，不进入错误文本。
        self.response_body = response_body


class AuthRequiredError(CollectorError):
    """Signal that the current persistent profile cannot authenticate requests."""


class HttpRequestError(CollectorError):
    """Signal a timeout or network failure before a valid response exists."""


class HttpResponseError(CollectorError):
    """Signal an HTTP or JSON response that cannot be accepted."""


class ResponseContractError(CollectorError):
    """Signal a successful JSON response that violates the verified contract."""
