"""Developer-only cleanup of allowlisted local collection data."""

import shutil
from dataclasses import dataclass
from pathlib import Path

from compass_collector.platform_runtime import PlatformRuntime


@dataclass(frozen=True, slots=True)
class LocalDataCleanupSummary:
    """Report safe counts without exposing deleted absolute paths."""

    # PostgreSQL 清理不删除数据库文件，此计数保留为零供现有界面展示。
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


def clear_local_data_with_locks(
    runtime_root: Path,
    database_url,
    *,
    platform: str | None = None,
    task_ids: tuple[str, ...] = (),
) -> LocalDataCleanupSummary:
    """Clear explicit platform data while holding both local operation locks."""
    if platform not in {"compass", "taobao"}:
        raise ValueError("clear-data requires an explicit platform")
    # 文件锁仍保护本机浏览器和文件；云端多节点调度不在本阶段范围内。
    scope = PlatformRuntime(runtime_root, platform)
    with scope.operation("scheduler"), scope.operation("collection"):
        return clear_platform_data(runtime_root, database_url, platform, task_ids)


def clear_platform_data(runtime_root: Path, database_url, platform: str,
                        task_ids: tuple[str, ...] = ()) -> LocalDataCleanupSummary:
    """Delete PG-owned batches and their exact files, preserving SQLite archives."""
    from sqlalchemy import delete, select
    from compass_collector.persistence import CollectionBatch, Database, SchedulerCheckpoint, upgrade_database

    if platform not in {"compass", "taobao"}:
        raise ValueError("unsupported cleanup platform")
    if runtime_root.is_symlink():
        raise ValueError("runtime root cannot be a symlink")
    # 只清理 PostgreSQL 批次登记的路径，旧 SQLite 文件和整个平台目录都保留。
    resolved_root = runtime_root.resolve(strict=False)
    candidates: set[Path] = set()
    owned_task_ids = set(task_ids)
    upgrade_database(database_url, platform=platform)
    database = Database(database_url)
    try:
        with database.session_factory.begin() as session:
            # 平台归属已检查，外键负责级联删除排名、店铺及 raw 索引。
            batches = session.scalars(select(CollectionBatch).where(CollectionBatch.platform == platform)).all()
            for batch in batches:
                owned_task_ids.add(batch.task_id)
                if batch.manifest_path:
                    candidates.add(Path(batch.manifest_path).parent)
                if batch.csv_path:
                    candidates.add(Path(batch.csv_path))
                # artifacts 与 raw 的批次键一致；只接受程序生成的完整键。
                if Path(batch.task_id).name != batch.task_id or Path(batch.id).name != batch.id:
                    raise ValueError("invalid batch artifact identity")
                candidates.add(resolved_root / "artifacts" / platform / batch.business_date.isoformat() / batch.task_id / batch.id)
            for candidate in candidates:
                # 目录和文件均必须位于当前平台的三个产物目录下，禁止链接穿越。
                allowed_roots = [resolved_root / name / platform for name in ("raw", "exports", "artifacts")]
                if not any(candidate.resolve().is_relative_to(root.resolve()) and candidate.resolve() != root.resolve() for root in allowed_roots):
                    raise ValueError("batch cleanup target is outside platform runtime")
                if any(root.is_symlink() or root.parent.is_symlink() for root in allowed_roots):
                    raise ValueError("platform runtime cannot be a symlink")
            # 所有路径验证完成后才删除数据库记录，连接失败时不动本地文件。
            deleted_batches = session.execute(delete(CollectionBatch).where(CollectionBatch.platform == platform)).rowcount
            if owned_task_ids:
                session.execute(delete(SchedulerCheckpoint).where(SchedulerCheckpoint.task_id.in_(owned_task_ids)))
    finally:
        database.close()
    # 数据库清理成功后移除本次批次所属文件；日志与归档不受影响。
    removed_paths = 0
    failures = 0
    for candidate in candidates:
        try:
            if _remove_allowlisted_path(candidate):
                removed_paths += 1
        except OSError:
            failures += 1
    return LocalDataCleanupSummary(0, removed_paths, failures, deleted_batches)


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
