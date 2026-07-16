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


class CollectionInterruptedError(CollectorError):
    """Signal a cooperative developer interruption without treating it as a failure."""


class PublicationError(CollectorError):
    """Signal a database or CSV publication failure without leaking row data."""


class BrowserOperationError(CollectorError):
    """Carry a safe browser failure snapshot without retaining exception text."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        failed_step: str,
        exception_type: str,
        safe_page_path: str | None = None,
        page_title: str | None = None,
        screenshot: bytes | None = None,
    ) -> None:
        """Store only page metadata approved for failure.json and failure.png."""

        super().__init__(message, category=category)
        # 失败步骤使用内部稳定名称，不保存 Playwright 调用参数。
        self.failed_step = failed_step
        # 异常类型不包含可能携带 URL 的异常文本。
        self.exception_type = exception_type
        # 页面路径固定为已批准的安全入口路径。
        self.safe_page_path = safe_page_path
        # 页面标题来自可见页面并限制长度。
        self.page_title = page_title[:200] if page_title else None
        # 截图仅在页面已经创建且捕获成功时存在。
        self.screenshot = screenshot


class TaskCollectionError(Exception):
    """Carry a failed run's local storage to the orchestration boundary."""

    def __init__(self, cause: CollectorError, storage: Any) -> None:
        """Wrap a safe collector error without changing its message or category."""

        super().__init__(str(cause))
        # 原始安全错误用于区分鉴权、契约和网络失败。
        self.cause = cause
        # 运行存储对象仅用于记录失败 run，不包含 Cookie。
        self.storage = storage
