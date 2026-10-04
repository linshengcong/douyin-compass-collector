"""PostgreSQL cleanup preserves historical files and rejects unscoped commands."""

import pytest
from pg_support import pg_url
from compass_collector.local_data import clear_local_data_with_locks
from compass_collector.runtime_locks import RuntimeLockBusy
from compass_collector.platform_runtime import PlatformRuntime


def test_cleanup_requires_platform_before_connecting(tmp_path):
    """An unscoped cleanup cannot touch databases or files."""
    # 旧文件只作为归档保留，不作为连接输入。
    archive = tmp_path / "collector.db"
    archive.write_bytes(b"archive")
    with pytest.raises(ValueError, match="explicit platform"):
        clear_local_data_with_locks(tmp_path, archive)
    assert archive.read_bytes() == b"archive"


@pytest.mark.parametrize("role", ["scheduler", "collection"])
def test_cleanup_refuses_active_local_operation(tmp_path, role):
    """An occupied operation prevents connection and deletion."""
    # 无效连接确保测试只能通过锁拒绝路径。
    scope = PlatformRuntime(tmp_path, "compass")
    with scope.operation(role):
        with pytest.raises(RuntimeLockBusy):
            clear_local_data_with_locks(tmp_path, "invalid", platform="compass")


def test_cleanup_empty_pg_preserves_sqlite_and_unregistered_files(tmp_path):
    """An empty PG store cannot delete archives or historical raw evidence."""
    # 模拟当前 runtime 下保留的数据库、响应、日志和登录目录。
    preserved = [tmp_path / "data/collector.db", tmp_path / "raw/compass/old/page.json.gz", tmp_path / "logs/old.jsonl", tmp_path / "browser-profile/Login Data"]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    result = clear_local_data_with_locks(tmp_path, pg_url(tmp_path), platform="compass")
    assert result.succeeded and result.database_batches == 0
    assert all(path.read_bytes() == b"keep" for path in preserved)


def test_cleanup_refuses_symlinked_runtime_before_connecting(tmp_path):
    """A linked root cannot redirect cleanup outside the runtime."""
    # 外部目录只参与路径检查，不执行数据库操作。
    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        clear_local_data_with_locks(alias, "invalid", platform="compass")
