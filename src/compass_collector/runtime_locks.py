"""Advisory process locks for GUI, Scheduler, and Chrome-backed collection work."""

import errno
import json
import os
from threading import Lock
from datetime import datetime
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo


# 锁文件元数据统一使用北京时间，便于人工排查占用进程。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 同一进程内的第二个同路径锁也必须失败；Windows 字节锁不保证该语义。
_OWNED_PATHS: set[Path] = set()
_OWNED_PATHS_LOCK = Lock()


class RuntimeLockBusy(RuntimeError):
    """Signal that another process already owns one runtime responsibility."""

    def __init__(self, role: str) -> None:
        """Store only a safe role name without exposing process arguments."""

        # role 只使用仓库内固定名称，可安全展示给终端或 GUI。
        self.role = role
        super().__init__(f"{role} is already running")


class ProcessLock:
    """Hold one non-blocking cross-platform advisory lock for the process lifetime."""

    def __init__(self, path: Path, role: str) -> None:
        """Configure a lock without acquiring or creating its file yet."""

        # path 是 runtime 下的稳定锁文件位置。
        self.path = path
        # role 是可安全写入锁元数据的固定职责名称。
        self.role = role
        # 文件句柄存在即代表当前对象持有锁。
        self._handle: IO[str] | None = None
        # _owned_path 在同一进程内维护互斥，并在释放时移除。
        self._owned_path: Path | None = None

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
        owned_path = self.path.resolve()
        with _OWNED_PATHS_LOCK:
            if owned_path in _OWNED_PATHS:
                raise RuntimeLockBusy(self.role)
            _OWNED_PATHS.add(owned_path)
        # a+ 避免在确认锁归属前截断其他进程的诊断元数据。
        lock_handle = self.path.open("a+", encoding="utf-8")
        try:
            _acquire_file_lock(lock_handle)
        except _LockUnavailableError as error:
            lock_handle.close()
            _release_owned_path(owned_path)
            raise RuntimeLockBusy(self.role) from error
        except OSError:
            # 其他系统错误也必须关闭尚未归属当前对象的文件句柄。
            lock_handle.close()
            _release_owned_path(owned_path)
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
        self._owned_path = owned_path

    def release(self) -> None:
        """Release the advisory lock and close its owning file descriptor."""

        # 重复释放是安全的，便于 finally 路径统一清理。
        lock_handle = self._handle
        if lock_handle is None:
            return
        try:
            _release_file_lock(lock_handle)
        finally:
            lock_handle.close()
            self._handle = None
            if self._owned_path is not None:
                _release_owned_path(self._owned_path)
                self._owned_path = None

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


class _LockUnavailableError(OSError):
    """Normalize platform-specific non-blocking lock conflicts."""


def _is_windows() -> bool:
    """Return whether the active interpreter uses the Windows file-lock API."""

    return os.name == "nt"


def _acquire_file_lock(lock_handle: IO[str]) -> None:
    """Acquire one byte-range lock on Windows or flock lock on POSIX."""

    if _is_windows():
        # msvcrt is Windows-only, so importing lazily keeps the module importable on macOS.
        import msvcrt

        lock_handle.seek(0)
        if not lock_handle.read(1):
            # Windows locking requires an existing byte range.
            lock_handle.seek(0)
            lock_handle.write(" ")
            lock_handle.flush()
        lock_handle.seek(0)
        try:
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                error,
                "winerror",
                None,
            ) in {32, 33}:
                raise _LockUnavailableError(str(error)) from error
            raise
        return

    # fcntl is unavailable on Windows and must never be imported at module load time.
    import fcntl

    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise _LockUnavailableError(str(error)) from error


def _release_file_lock(lock_handle: IO[str]) -> None:
    """Release the matching platform-specific advisory lock."""

    if _is_windows():
        import msvcrt

        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _release_owned_path(path: Path) -> None:
    """Forget a process-local reservation after failed acquisition or release."""

    with _OWNED_PATHS_LOCK:
        _OWNED_PATHS.discard(path)
