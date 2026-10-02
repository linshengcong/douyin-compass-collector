"""Verify independent platform resources with real locks, databases and Qt windows."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from compass_collector.config import AppConfig, load_config
from compass_collector.persistence import Database, upgrade_database
from compass_collector.platform_runtime import PlatformRuntime
from compass_collector.retention import cleanup_runtime
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy
from compass_collector.runtime_logging import RuntimeLogger, read_latest_batch_events


@pytest.mark.parametrize("role", ["gui", "collection", "scheduler"])
def test_platform_roles_can_overlap_but_same_platform_is_exclusive(tmp_path, role):
    """Different platforms overlap while every same-platform responsibility stays exclusive."""
    # 两平台同时持有真实文件锁，不用预置锁文件模拟占用。
    first = PlatformRuntime(tmp_path, "compass")
    second = PlatformRuntime(tmp_path, "taobao")
    with first.operation(role), second.operation(role):
        with pytest.raises(RuntimeLockBusy):
            with first.operation(role):
                pytest.fail("duplicate operation entered")
    with first.operation(role):
        pass


def test_profile_resource_cannot_be_reopened_through_other_platform(tmp_path):
    """Canonical Profile identity protects two independent single-platform configs."""
    # 相对路径和含..的别名最终定位同一资源。
    first = PlatformRuntime(tmp_path, "compass")
    second = PlatformRuntime(tmp_path, "taobao")
    profile = tmp_path / "profile"
    with first.operation("collection", profile):
        with pytest.raises(RuntimeLockBusy, match="profile"):
            with second.operation("collection", tmp_path / "unused" / ".." / "profile"):
                pytest.fail("profile reopened")


@pytest.mark.parametrize("role", ["gui", "collection", "scheduler"])
def test_abrupt_process_exit_releases_platform_lock(tmp_path, role):
    """Recover scoped OS locks even when a child exits without running finally."""
    # 子进程通过os._exit跳过上下文清理，验证残留锁文件不会永久占用平台。
    code = """
import os, sys
from pathlib import Path
from compass_collector.platform_runtime import PlatformRuntime
with PlatformRuntime(Path(sys.argv[1]), 'taobao').operation(sys.argv[2]):
    os._exit(9)
"""
    # 测试进程只访问临时目录，不能触及真实GUI或已登录Profile。
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), role],
                            env={**os.environ, "PYTHONPATH": "src"},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 9, result.stderr
    with PlatformRuntime(tmp_path, "taobao").operation(role):
        pass


@pytest.mark.parametrize("role", ["gui", "collection", "scheduler"])
def test_old_global_process_blocks_new_runtime_without_being_stopped(tmp_path, role):
    """Legacy locks are probed through the OS instead of trusting stale metadata."""
    # 新进程只提示旧进程占用，不能删除锁文件或终止进程。
    with ProcessLock(tmp_path / "locks" / f"{role}.lock", role):
        with pytest.raises(RuntimeLockBusy, match="legacy"):
            with PlatformRuntime(tmp_path, "taobao").operation("collection"):
                pytest.fail("legacy conflict ignored")


def test_database_claim_rejects_another_platform_even_when_empty(tmp_path):
    """Independent configs cannot reuse an empty database claimed by another platform."""
    # 没有商品或批次时也必须保留平台身份。
    path = tmp_path / "data.db"
    upgrade_database(path, platform="compass")
    with pytest.raises(ValueError, match="another platform"):
        upgrade_database(path, platform="taobao")
    # 重复初始化不创建新备份，不重新执行迁移。
    upgrade_database(path, platform="compass")
    assert not (tmp_path / "backups").exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id,platform FROM runtime_platform").fetchall() == [(1, "compass")]


def test_mixed_history_is_rejected_without_modifying_database(tmp_path):
    """Preflight refuses mixed source history before migrations or identity claims."""
    # 创建历史混合库，只检验拒绝行为，不调用真实采集或发布。
    path = tmp_path / "mixed.db"
    upgrade_database(path)
    database = Database(path)
    for platform in ("compass", "taobao"):
        database.create_batch(batch_id=platform, task_id=platform, platform=platform,
                              business_date=datetime(2026, 10, 2).date(),
                              planned_at=datetime(2026, 10, 2), mode="force", brand_type=None,
                              price_bin=None, manifest_path=tmp_path / platform,
                              started_at=datetime(2026, 10, 2))
    database.close()
    # 原数据库字节内容及归属表在失败后保持不变。
    before = path.read_bytes()
    with pytest.raises(ValueError, match="mixed platform"):
        upgrade_database(path, platform="taobao")
    assert path.read_bytes() == before


def test_concurrent_initialization_of_same_database_is_serialized(tmp_path):
    """Real processes race on initialization while only one schema is created."""
    # 四个进程独立运行迁移，验证数据库锁覆盖首次建库及归属认领。
    path = tmp_path / "concurrent.db"
    code = "from pathlib import Path; from compass_collector.persistence import upgrade_database; import sys; upgrade_database(Path(sys.argv[1]), platform='taobao')"
    environment = {**os.environ, "PYTHONPATH": "src"}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path)], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT platform FROM runtime_platform").fetchone() == ("taobao",)


def test_real_processes_do_not_mistake_parallel_legacy_probes_for_old_gui(tmp_path):
    """Stress simultaneous startup checks that previously falsely reported legacy_gui."""
    # 两个真实进程频繁探测相同旧锁，再各自持有独立平台锁。
    code = """
import sys, time
from pathlib import Path
from compass_collector.platform_runtime import PlatformRuntime
scope = PlatformRuntime(Path(sys.argv[1]), sys.argv[2])
for attempt in range(80):
    with scope.operation('collection'):
        time.sleep(0.002)
"""
    environment = {**os.environ, "PYTHONPATH": "src"}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), platform],
                                  env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for platform in ("compass", "taobao")]
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr


def test_owned_database_refuses_foreign_batch_writes(tmp_path):
    """An entrypoint cannot contaminate a claimed database after initialization."""
    # 同一业务事务内再核验归属，不仅依赖配置加载阶段。
    path = tmp_path / "owned.db"
    upgrade_database(path, platform="taobao")
    database = Database(path)
    try:
        with pytest.raises(ValueError, match="belongs to another platform"):
            database.create_batch(batch_id="foreign", task_id="foreign", platform="compass",
                                  business_date=datetime(2026, 10, 2).date(),
                                  planned_at=datetime(2026, 10, 2), mode="force", brand_type=None,
                                  price_bin=None, manifest_path=tmp_path / "manifest.json",
                                  started_at=datetime(2026, 10, 2))
        assert database.recent_status() == []
    finally:
        database.close()


def test_config_paths_and_legacy_single_platform_database(tmp_path):
    """Validate canonical separation while retaining old single-platform YAML support."""
    # 主配置的两个数据库独立；旧单平台PoC仍使用顶层路径。
    config = load_config(Path("config/tasks.yaml"))
    assert config.for_platform("compass").database.path != config.for_platform("taobao").database.path
    legacy = load_config(Path("config/taobao-poc.yaml"))
    assert legacy.for_platform("taobao").database.path == legacy.database.path
    # 路径别名不能绕过配置校验，也不能遗漏多平台数据库字段。
    raw = config.model_dump(mode="python")
    raw["platforms"]["taobao"]["database_path"] = raw["platforms"]["compass"]["database_path"]
    with pytest.raises(ValidationError, match="databases must be distinct"):
        AppConfig.model_validate(raw)
    raw["platforms"]["taobao"]["database_path"] = None
    with pytest.raises(ValidationError, match="independent database_path"):
        AppConfig.model_validate(raw)


def test_portable_path_mapping_cannot_create_database_aliases(tmp_path, monkeypatch):
    """Validate actual portable paths after relative runtime values are relocated."""
    from compass_collector import config as config_module
    # 映射前路径不同，映射后两个数据库相同，必须再次校验。
    raw = yaml.safe_load(Path("config/tasks.yaml").read_text())
    raw["platforms"]["taobao"]["database_path"] = str(tmp_path / "data/collector.db")
    path = tmp_path / "portable.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(config_module, "is_packaged_application", lambda: True)
    monkeypatch.setattr(config_module, "runtime_root", lambda: tmp_path)
    with pytest.raises(ValidationError, match="databases must be distinct"):
        load_config(path)


def test_logs_and_retention_never_cross_platforms(tmp_path):
    """Newest run lookup and expired-material cleanup remain platform-local."""
    # 同一天两个平台的不同批次必须落到不同JSONL，不拼接共享日志。
    for platform in ("compass", "taobao"):
        scope = PlatformRuntime(tmp_path, platform)
        RuntimeLogger(scope.logs, platform=platform, execution_batch_id=platform).emit(
            level="INFO", event="started", message=platform, stage="test")
        assert read_latest_batch_events(scope.logs)[0]["platform"] == platform
        for name in ("raw", "artifacts", "logs"):
            candidate = tmp_path / name / platform / ("2000-01-01.jsonl" if name == "logs" else "2000-01-01")
            if name == "logs":
                candidate.write_text("old")
            else:
                candidate.mkdir(parents=True)
    # 旧共享日志和旧另一平台任务目录也必须保留。
    shared = tmp_path / "logs" / "2000-01-01.jsonl"
    shared.write_text("legacy")
    legacy = tmp_path / "raw" / "2000-01-01" / "taobao_task"
    legacy.mkdir(parents=True)
    config = load_config(Path("config/tasks.yaml"))
    cleanup_runtime(tmp_path, config.retention, platform="compass", task_ids=("compass_task",))
    assert not (tmp_path / "raw/compass/2000-01-01").exists()
    assert (tmp_path / "raw/taobao/2000-01-01").exists()
    assert shared.exists() and legacy.exists()


def test_two_actual_gui_windows_have_independent_titles_and_scheduler_status(tmp_path, monkeypatch):
    """Construct real offscreen Qt windows and verify one platform cannot disable the other."""
    # Qt只渲染到内存，不启动Chrome、采集、通知或后台服务。
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from compass_collector import gui
    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "RUNTIME_ROOT", tmp_path)
    config = load_config(Path("config/tasks.yaml"))
    windows = []
    try:
        for platform in ("compass", "taobao"):
            selected = config.for_platform(platform)
            # 两窗口的数据库均处于pytest目录，登录Profile只读展示。
            selected = selected.model_copy(update={
                "database": selected.database.model_copy(update={"path": tmp_path / "data" / f"{platform}.db"}),
                "platforms": {platform: selected.platforms[platform].model_copy(update={"database_path": None})},
            })
            request = gui.GuiLaunchRequest(config_path=Path("config/tasks.yaml"), platform=platform, task_id=selected.tasks[0].id, auto_start=False)
            windows.append(gui.CollectorWindow(selected, request))
        assert windows[0].windowTitle() != windows[1].windowTitle()
        with PlatformRuntime(tmp_path, "taobao").operation("scheduler"):
            windows[0]._refresh_scheduler_status()
            windows[1]._refresh_scheduler_status()
            assert windows[0].scheduler_status_label.text() == "未运行"
            assert "外部" in windows[1].scheduler_status_label.text()
        windows[0].close()
        assert windows[1].runtime_scope.platform == "taobao"
    finally:
        for window in windows:
            window.close()
        application.processEvents()
