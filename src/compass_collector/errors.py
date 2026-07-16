"""Safe error types that never embed request URLs, headers, or Cookie values."""

from typing import Any


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


class PublicationError(CollectorError):
    """Signal a database or CSV publication failure without leaking row data."""


class TaskCollectionError(Exception):
    """Carry a failed run's local storage to the orchestration boundary."""

    def __init__(self, cause: CollectorError, storage: Any) -> None:
        """Wrap a safe collector error without changing its message or category."""

        super().__init__(str(cause))
        # 原始安全错误用于区分鉴权、契约和网络失败。
        self.cause = cause
        # 运行存储对象仅用于记录失败 run，不包含 Cookie。
        self.storage = storage
