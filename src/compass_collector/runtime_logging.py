"""Safe console and daily JSON Lines logging for collector runs."""

import json
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo


# 日志日期和时间戳统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 只有明确审查过的诊断字段可以进入 JSONL。
SAFE_DETAIL_FIELDS = {
    "artifact_path",
    "authentication_item_count",
    "batch_status",
    "cleanup_counts",
    "csv_path",
    "delay_seconds",
    "dry_run",
    "error_category",
    "page_no",
    "planned_at",
    "saved_items",
    "status_code",
    "target_items",
    "target_pages",
    "version",
}
# 日志正文和详情出现这些认证标记时直接拒绝写入。
FORBIDDEN_TEXT_MARKERS = (
    "authorization",
    "cookie",
    "sessionid",
    "mstoken",
    "a_bogus",
    "verifyfp",
    "verify_fp",
)
# GUI Scheduler 子进程使用固定前缀传输同一份安全事件。
EVENT_STREAM_PREFIX = "@@COMPASS_EVENT@@"
# 环境变量只切换控制台编码方式，不改变持久化 JSONL。
EVENT_STREAM_ENV = "COMPASS_EVENT_STREAM"
# 安全事件订阅者只能收到完成字段审查后的 payload 副本。
EventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class LogContext:
    """Identify one task attempt in every task-scoped log entry."""

    # 执行批次 ID 与数据库自增主键解耦，启动时即可用于串联日志。
    batch_id: str
    # 每次任务尝试的 run_id 与原始响应目录一致。
    run_id: str | None = None
    # 任务 ID 用于多任务运行时过滤日志。
    task_id: str | None = None


class RuntimeLogger:
    """Append safe structured events to one Beijing-date JSONL file."""

    def __init__(self, log_directory: Path, event_sink: EventSink | None = None) -> None:
        """Create the daily log directory without opening a long-lived handle."""

        # 日志目录在第一次事件写入前创建。
        self.log_directory = log_directory
        self.log_directory.mkdir(parents=True, exist_ok=True)
        # event_sink 用于当前进程 GUI 实时展示，不拥有第二套日志。
        self.event_sink = event_sink

    def _log_path(self, captured_at: datetime) -> Path:
        """Return the natural daily-rotation path for one event timestamp."""

        # 文件名只包含北京时间日期，跨日后自动切换新文件。
        local_time = captured_at.astimezone(SHANGHAI_TIMEZONE)
        return self.log_directory / f"{local_time.date().isoformat()}.jsonl"

    def emit(
        self,
        *,
        level: Literal["INFO", "WARNING", "ERROR"],
        event: str,
        message: str,
        stage: str,
        context: LogContext | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Write one allowlisted event and mirror its readable message to console."""

        # 调用方未提供详情时使用空映射，避免可变默认参数。
        safe_details = details or {}
        # 未登记字段可能携带请求或认证上下文，因此直接拒绝。
        unknown_fields = set(safe_details) - SAFE_DETAIL_FIELDS
        if unknown_fields:
            raise ValueError(f"unsafe log detail fields: {sorted(unknown_fields)}")
        # 事件、消息、阶段和详情统一做认证标记检查。
        candidate_text = json.dumps(
            [event, message, stage, safe_details],
            ensure_ascii=False,
            default=str,
        ).lower()
        # 任一认证标记命中都拒绝写入，避免未来误传异常文本。
        matched_markers = [
            marker for marker in FORBIDDEN_TEXT_MARKERS if marker in candidate_text
        ]
        if matched_markers:
            raise ValueError(f"sensitive markers in log event: {matched_markers}")
        # 单条日志时间戳只计算一次，文件名与正文保持一致。
        captured_at = datetime.now(SHANGHAI_TIMEZONE)
        # 无任务上下文的系统事件显式输出 null，不伪造 run_id。
        payload: dict[str, Any] = {
            "timestamp": captured_at.isoformat(),
            "level": level,
            "event": event,
            "message": message,
            "batch_id": context.batch_id if context else None,
            "run_id": context.run_id if context else None,
            "task_id": context.task_id if context else None,
            "stage": stage,
            **safe_details,
        }
        # 每次完整追加一行并立即关闭句柄，便于崩溃后排查。
        log_path = self._log_path(captured_at)
        with log_path.open("a", encoding="utf-8") as file_handle:
            file_handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            file_handle.write("\n")
        if self.event_sink is not None:
            # 回调收到副本，避免订阅者意外修改后续控制台输出。
            self.event_sink(dict(payload))
        if os.environ.get(EVENT_STREAM_ENV) == "1":
            # GUI 子进程只输出带前缀的安全 JSON，避免消息重复展示。
            print(
                EVENT_STREAM_PREFIX
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                flush=True,
            )
        else:
            print(message)


def read_latest_batch_events(log_directory: Path, limit: int = 500) -> list[dict[str, Any]]:
    """Read at most one latest batch of persisted safe JSONL events."""

    if limit <= 0 or not log_directory.exists():
        return []
    # 保留窗口内文件按日期排序后顺序读取，最终定位最新批次。
    all_events: deque[dict[str, Any]] = deque()
    for log_path in sorted(log_directory.glob("*.jsonl")):
        # 单行损坏不应阻止 GUI 查看其他已落盘安全事件。
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                all_events.append(event)
    # 从最新事件向前查找最后一个真实采集批次 ID。
    latest_batch_id = next(
        (event.get("batch_id") for event in reversed(all_events) if event.get("batch_id")),
        None,
    )
    if latest_batch_id is None:
        return list(all_events)[-limit:]
    # 只恢复最近批次，系统级 Scheduler 日志由实时通道继续追加。
    batch_events = [event for event in all_events if event.get("batch_id") == latest_batch_id]
    return batch_events[-limit:]
