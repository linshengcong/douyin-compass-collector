"""Developer-only cleanup of allowlisted local collection data."""

import shutil
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from compass_collector.runtime_locks import ProcessLock
from compass_collector.platform_runtime import PlatformRuntime


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
    # 平台清理保留数据库文件，只统计删除的顶层批次。
    database_batches: int = 0

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
    *,
    platform: str | None = None,
    task_ids: tuple[str, ...] = (),
) -> LocalDataCleanupSummary:
    """Clear local data only while Scheduler and collection locks are both owned."""

    # 无平台的底层旧API必须同时锁住两平台；CLI已禁止这种入口。
    names = (platform,) if platform is not None else ("compass", "taobao")
    with ExitStack() as locks:
        for name in names:
            scope = PlatformRuntime(runtime_root, name)
            locks.enter_context(scope.operation("scheduler"))
            locks.enter_context(scope.operation("collection"))
        if platform is not None:
            return clear_platform_data(runtime_root, database_path, platform, task_ids)
        return clear_local_data(runtime_root, database_path)


def clear_platform_data(runtime_root: Path, database_path: Path, platform: str,
                        task_ids: tuple[str, ...] = ()) -> LocalDataCleanupSummary:
    """Delete one platform's batch rows and files, preserving shared logs and login data."""
    if platform not in {"compass", "taobao"}:
        raise ValueError("unsupported cleanup platform")
    # 先验证数据库和平台目录边界，不能顺着符号链接清理其他工程。
    resolved_database = _validated_database_path(runtime_root, database_path)
    resolved_root = runtime_root.resolve(strict=False)
    # 新平台目录覆盖raw/CSV/失败材料及本地网站发布暂存，不触及远端文件。
    candidates = [resolved_root / name / platform for name in ("exports", "raw", "artifacts", "web-publication")]
    for candidate in candidates:
        if candidate.parent.is_symlink() or not candidate.resolve().is_relative_to(resolved_root):
            raise ValueError("platform cleanup target is outside runtime")
    # 数据库不存在时也允许清理残留文件；不创建新数据库或删除其他平台的记录。
    deleted_batches = 0
    owned_task_ids = set(task_ids)
    if resolved_database.exists():
        from compass_collector.persistence import upgrade_database

        upgrade_database(resolved_database, platform=platform)
        with sqlite3.connect(resolved_database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            owned_task_ids.update(row[0] for row in connection.execute(
                "SELECT DISTINCT task_id FROM collection_batches WHERE platform=?", (platform,)))
            # 旧目录只有task维度，另一平台复用了相同task时必须保留。
            owned_task_ids.difference_update(row[0] for row in connection.execute(
                "SELECT DISTINCT task_id FROM collection_batches WHERE platform<>?", (platform,)))
            # 外键级联删除分类、raw索引、正式商品及店铺，保留另一平台全部记录。
            deleted_batches = connection.execute("DELETE FROM collection_batches WHERE platform=?", (platform,)).rowcount
            for task_id in owned_task_ids:
                connection.execute("DELETE FROM scheduler_checkpoints WHERE task_id=?", (task_id,))
    # 兼容原抖音日期/task目录，要求task只是一段目录名，避免配置值成为路径穿越。
    for name in ("exports", "raw", "artifacts"):
        for day_directory in (resolved_root / name).glob("????-??-??"):
            if day_directory.is_symlink():
                continue
            for task_id in owned_task_ids:
                if PlatformRuntime(resolved_root, platform).owns_legacy_task(day_directory.name, task_id):
                    candidates.append(day_directory / task_id)
    # 共享JSONL包含其他平台审计，保留整个logs目录。
    removed_directories = 0
    failures = 0
    for candidate in candidates:
        try:
            if _remove_allowlisted_path(candidate):
                removed_directories += 1
        except OSError:
            failures += 1
    return LocalDataCleanupSummary(0, removed_directories, failures, deleted_batches)


def clear_auth_data(profile_dir: Path) -> bool:
    """Remove the Chrome browser profile directory and recreate it as empty."""

    # profile_dir 来自已校验的配置，不跟随符号链接。
    resolved_profile = profile_dir.resolve(strict=False)
    if not resolved_profile.exists():
        return False
    # 防删除 runtime 之外的路径：要求 profile 是已知 runtime 子目录。
    shutil.rmtree(resolved_profile)
    resolved_profile.mkdir(parents=True, exist_ok=True)
    return True


def clear_auth_data_with_locks(
    runtime_root: Path,
    profile_dir: Path,
    *,
    platform: str = "compass",
) -> bool:
    """Remove the Chrome profile only while collection lock is owned."""

    # Profile资源锁防止另一份配置以不同平台名称占用相同目录。
    scope = PlatformRuntime(runtime_root, platform)
    with scope.operation("collection", profile_dir):
        return clear_auth_data(profile_dir)
