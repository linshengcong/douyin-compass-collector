"""Advisory process locks for GUI, Scheduler, and Chrome-backed collection work."""

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo


# 锁文件元数据统一使用北京时间，便于人工排查占用进程。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class RuntimeLockBusy(RuntimeError):
    """Signal that another process already owns one runtime responsibility."""

    def __init__(self, role: str) -> None:
        """Store only a safe role name without exposing process arguments."""

        # role 只使用仓库内固定名称，可安全展示给终端或 GUI。
        self.role = role
        super().__init__(f"{role} is already running")


class ProcessLock:
    """Hold one non-blocking macOS advisory lock for the process lifetime."""

    def __init__(self, path: Path, role: str) -> None:
        """Configure a lock without acquiring or creating its file yet."""

        # path 是 runtime 下的稳定锁文件位置。
        self.path = path
        # role 是可安全写入锁元数据的固定职责名称。
        self.role = role
        # 文件句柄存在即代表当前对象持有锁。
        self._handle: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        """Return whether this object currently owns the advisory lock."""

        return self._handle is not None

    def acquire(self) -> None:
        """Acquire immediately or raise when another process owns the lock."""

        if self._handle is not None:
            return
        # 锁目录只存放无敏感信息的进程协调文件。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # a+ 避免在确认锁归属前截断其他进程的诊断元数据。
        lock_handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_handle.close()
            raise RuntimeLockBusy(self.role) from error
        except OSError:
            # 其他系统错误也必须关闭尚未归属当前对象的文件句柄。
            lock_handle.close()
            raise
        # 当前 PID 和固定职责足以辅助排查，不写命令行或环境变量。
        metadata = {
            "pid": os.getpid(),
            "role": self.role,
            "started_at": datetime.now(SHANGHAI_TIMEZONE).isoformat(),
        }
        lock_handle.seek(0)
        lock_handle.truncate()
        json.dump(metadata, lock_handle, ensure_ascii=False, separators=(",", ":"))
        lock_handle.write("\n")
        lock_handle.flush()
        # 句柄必须保持打开，操作系统才会继续持有 advisory lock。
        self._handle = lock_handle

    def release(self) -> None:
        """Release the advisory lock and close its owning file descriptor."""

        # 重复释放是安全的，便于 finally 路径统一清理。
        lock_handle = self._handle
        if lock_handle is None:
            return
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()
            self._handle = None

    def __enter__(self) -> "ProcessLock":
        """Acquire this lock for a context-managed operation."""

        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Always release the lock when leaving its operation boundary."""

        self.release()


def lock_is_held(path: Path, role: str) -> bool:
    """Probe one advisory lock without relying on possibly stale PID metadata."""

    # 临时对象成功加锁说明当前没有其他进程占用。
    probe = ProcessLock(path, role)
    try:
        probe.acquire()
    except RuntimeLockBusy:
        return True
    finally:
        probe.release()
    return False
