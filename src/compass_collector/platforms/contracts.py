"""Small platform interface consumed by serial task orchestration."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Protocol

from compass_collector.models import CategoryDiscoveryResult, ProductRankEntry


@dataclass(frozen=True, slots=True)
class DiscoveryCapture:
    """Return category snapshots and their local-only discovery evidence."""

    # 规范化范围不依赖接口路径或请求参数格式。
    discovery: CategoryDiscoveryResult
    # 原始分类响应只供本地审计保存。
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PageCapture:
    """Return a validated page without exposing browser objects to callers."""

    # 页码和总数由适配器根据平台契约验证。
    page_no: int
    api_total: int
    target_page_count: int
    captured_at: datetime
    # 商品已转换为共享字段与实际金额、件数单位。
    entries: tuple[ProductRankEntry, ...]
    # 原始响应及业务参数分别用于受限审计和安全摘要。
    payload: dict[str, Any]
    safe_params: dict[str, str | int]


class PlatformAdapter(Protocol):
    """Own one platform session, discovery and complete serial scope capture."""

    def open_session(self, *, login_only: bool = False) -> None:
        """Open the platform page and resolve authentication."""

    def discover_scopes(self, task) -> DiscoveryCapture:
        """Validate all configured targets before ranking collection."""

    def collect_scope(self, task, scope, business_date) -> Iterator[PageCapture]:
        """Yield validated pages and reject an incomplete ranking at exhaustion."""

    def close(self) -> None:
        """Release listeners, Chrome and platform resources."""
