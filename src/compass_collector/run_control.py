"""Thread-safe cooperative controls shared by terminal, GUI, and Scheduler runs."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from typing import Any


# 安全日志事件回调只接收已经过 RuntimeLogger 审核的 payload。
SafeEventSink = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class CollectionControl:
    """Coordinate cancellation, browser inspection, and safe event delivery."""

    # GUI 事件通道只传输已脱敏的结构化日志。
    event_sink: SafeEventSink | None = None
    # GUI 手动运行结束后等待明确的关闭 Chrome 操作。
    keep_browser_open: bool = False
    # 中止信号在请求边界和分页等待期间协作式检查。
    _stop_event: Event = field(default_factory=Event, init=False, repr=False)
    # Chrome 关闭信号让工作线程在保留调试窗口时继续退出。
    _browser_close_event: Event = field(default_factory=Event, init=False, repr=False)

    def request_stop(self) -> None:
        """Request a cooperative stop at the next safe collection boundary."""

        self._stop_event.set()

    def stop_requested(self) -> bool:
        """Return whether a caller requested interruption."""

        return self._stop_event.is_set()

    def wait_for_delay(self, seconds: float) -> bool:
        """Wait for a page interval and return early when stop is requested."""

        return self._stop_event.wait(seconds)

    def request_browser_close(self) -> None:
        """Allow a retained manual Chrome session to close."""

        self._browser_close_event.set()

    def wait_for_browser_close(self) -> None:
        """Block the worker thread until the GUI finishes browser inspection."""

        self._browser_close_event.wait()
