"""Developer-only cleanup of allowlisted local collection data."""

import shutil
from dataclasses import dataclass
from pathlib import Path

from compass_collector.runtime_locks import ProcessLock


# 只有这些 runtime 一级目录可由调试清理功能删除并重建。
DISPOSABLE_RUNTIME_DIRECTORIES = ("exports", "raw", "artifacts", "logs")
# SQLite 主文件和同名 sidecar 作为一个调试数据集清理。
SQLITE_FILE_SUFFIXES = ("", "-wal", "-shm", "-journal")
# 清理与 Scheduler 共用这两个已有进程锁名称。
SCHEDULER_LOCK_NAME = "scheduler.lock"
COLLECTION_LOCK_NAME = "collection.lock"


@dataclass(frozen=True, slots=True)
class LocalDataCleanupSummary:
    """Report safe counts without exposing deleted absolute paths."""

    # database_files 统计实际删除的 SQLite 主文件和 sidecar。
    database_files: int
    # runtime_directories 统计实际清空的已知业务目录。
    runtime_directories: int
    # failures 只记录失败数量，不保留系统异常原文。
    failures: int

    @property
    def succeeded(self) -> bool:
        """Return whether every allowlisted deletion and recreation succeeded."""

        return self.failures == 0


def _validated_database_path(runtime_root: Path, database_path: Path) -> Path:
    """Resolve the configured database only inside the protected runtime data root."""

    # runtime 根目录不允许是符号链接，避免清理落到其他工程。
    if runtime_root.is_symlink():
        raise ValueError("runtime root cannot be a symlink")
    # runtime/data 是数据库唯一允许的删除边界。
    data_root = runtime_root / "data"
    if data_root.is_symlink():
        raise ValueError("runtime data root cannot be a symlink")
    # 数据库文件本身不允许是符号链接，防止删除 Profile 等受保护文件。
    if database_path.is_symlink():
        raise ValueError("database path cannot be a symlink")

    # resolve(strict=False) 同时消解 .. 和已存在的中间符号链接。
    resolved_data_root = data_root.resolve(strict=False)
    # 相对数据库路径按当前工程工作目录解析。
    resolved_database = database_path.resolve(strict=False)
    if (
        resolved_database == resolved_data_root
        or not resolved_database.is_relative_to(resolved_data_root)
    ):
        raise ValueError("database cleanup target must be inside runtime/data")
    return resolved_database


def _remove_allowlisted_path(candidate: Path) -> bool:
    """Remove one exact allowlisted file or directory without following root symlinks."""

    if candidate.is_symlink() or candidate.is_file():
        # 顶层符号链接只删除链接本身，不跟随到 runtime 之外。
        candidate.unlink()
        return True
    if candidate.is_dir():
        # shutil.rmtree 仅用于已经白名单确认的 runtime 子目录。
        shutil.rmtree(candidate)
        return True
    return False


def clear_local_data(
    runtime_root: Path,
    database_path: Path,
) -> LocalDataCleanupSummary:
    """Clear only known collection data while preserving login and coordination state."""

    # 先验证可配置数据库路径，任何删除都不得早于安全检查。
    resolved_root = runtime_root.resolve(strict=False)
    resolved_database = _validated_database_path(runtime_root, database_path)
    # 三类结果分开计数，GUI 和 CLI 只展示安全数字。
    deleted_database_files = 0
    deleted_runtime_directories = 0
    failure_count = 0

    for suffix in SQLITE_FILE_SUFFIXES:
        # sidecar 通过已验证主路径追加固定后缀，不接受用户输入。
        database_candidate = Path(f"{resolved_database}{suffix}")
        try:
            if _remove_allowlisted_path(database_candidate):
                deleted_database_files += 1
        except OSError:
            failure_count += 1

    for directory_name in DISPOSABLE_RUNTIME_DIRECTORIES:
        # 目录名来自模块常量白名单。
        directory_path = resolved_root / directory_name
        try:
            if _remove_allowlisted_path(directory_path):
                deleted_runtime_directories += 1
            # 清理后重建空目录，后续日志和采集可直接继续。
            directory_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            failure_count += 1

    try:
        # 数据库父目录保留，新库由下一次 status 或采集按迁移创建。
        resolved_database.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        failure_count += 1

    return LocalDataCleanupSummary(
        database_files=deleted_database_files,
        runtime_directories=deleted_runtime_directories,
        failures=failure_count,
    )


def clear_local_data_with_locks(
    runtime_root: Path,
    database_path: Path,
) -> LocalDataCleanupSummary:
    """Clear local data only while Scheduler and collection locks are both owned."""

    # 先获取 Scheduler 锁，防止新的计划批次在清理期间启动。
    scheduler_lock = ProcessLock(
        runtime_root / "locks" / SCHEDULER_LOCK_NAME,
        "scheduler",
    )
    # 再获取采集锁，防止 GUI、login 或终端 run 同时写入。
    collection_lock = ProcessLock(
        runtime_root / "locks" / COLLECTION_LOCK_NAME,
        "collection",
    )
    with scheduler_lock, collection_lock:
        return clear_local_data(runtime_root, database_path)
