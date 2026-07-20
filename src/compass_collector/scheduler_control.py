"""Cross-platform one-shot control files for an owned Scheduler process."""

from dataclasses import dataclass
from pathlib import Path
import re


# GUI 通过子进程环境传递本次 Scheduler 的随机实例 ID。
SCHEDULER_CONTROL_ID_ENV = "COMPASS_SCHEDULER_CONTROL_ID"
# 实例 ID 只接受 uuid4().hex 形式，不能构造 controls 目录外的路径。
CONTROL_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class SchedulerControlFiles:
    """Address one Scheduler instance through isolated one-shot request files."""

    # control_root 是所有 Scheduler 控制文件的持久运行目录。
    control_root: Path
    # instance_id 唯一对应一次 GUI 启动的 Scheduler 子进程。
    instance_id: str

    def __post_init__(self) -> None:
        """Reject unsafe or ambiguous instance identifiers."""

        if CONTROL_ID_PATTERN.fullmatch(self.instance_id) is None:
            raise ValueError("invalid scheduler control instance id")

    @property
    def shutdown_path(self) -> Path:
        """Return the graceful-shutdown request path for this instance."""

        return self.control_root / f"scheduler-{self.instance_id}.shutdown"

    @property
    def interruption_path(self) -> Path:
        """Return the active-collection interruption path for this instance."""

        return self.control_root / f"scheduler-{self.instance_id}.interrupt"

    @property
    def event_path(self) -> Path:
        """Return the safe Scheduler-to-GUI event stream path for this instance."""

        return self.control_root / f"scheduler-{self.instance_id}.events.jsonl"

    def request_shutdown(self) -> None:
        """Ask the Scheduler to stop future work after active work finishes."""

        self._write_request(self.shutdown_path)

    def request_interruption(self) -> None:
        """Ask the Scheduler to cooperatively stop only its current collection."""

        self._write_request(self.interruption_path)

    def consume_shutdown(self) -> bool:
        """Consume one graceful-shutdown request if it exists."""

        return self._consume_request(self.shutdown_path)

    def consume_interruption(self) -> bool:
        """Consume one active-collection interruption request if it exists."""

        return self._consume_request(self.interruption_path)

    def cleanup(self) -> None:
        """Remove any control requests left after the addressed Scheduler exits."""

        self._consume_request(self.shutdown_path)
        self._consume_request(self.interruption_path)

    def clear_event_log(self) -> None:
        """Remove the event log after the owning GUI reads its final entries."""

        self._consume_request(self.event_path)

    def _write_request(self, request_path: Path) -> None:
        """Create one same-filesystem marker without partial request contents."""

        self.control_root.mkdir(parents=True, exist_ok=True)
        request_path.touch(exist_ok=True)

    @staticmethod
    def _consume_request(request_path: Path) -> bool:
        """Atomically claim one marker by deleting its directory entry."""

        try:
            request_path.unlink()
        except FileNotFoundError:
            return False
        return True
