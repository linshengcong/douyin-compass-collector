"""Verify the public Make surface and real CLI boundaries without external actions."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from compass_collector import cli, gui
from compass_collector.config import load_config


# 常规Make检查使用-n；并行测试替换执行器，不采集、不发送、不清理、不安装服务。
PLATFORM_COMMANDS = ("run", "login", "status", "clean", "schedule", "notify", "check")


def make_output(*arguments):
    """Expand the production Makefile using an environment free of developer overrides."""
    # 清理可影响默认值的进程变量，保留PATH以调用真实make。
    environment = os.environ.copy()
    for name in ("PLATFORM", "MODE", "GUI", "START", "CONTINUE", "NOTIFY", "ACTION", "CONFIG", "TASK", "MAKEFLAGS"):
        environment.pop(name, None)
    return subprocess.run(["make", "-n", *arguments], capture_output=True, text=True, env=environment)


@pytest.mark.parametrize("command", PLATFORM_COMMANDS)
@pytest.mark.parametrize("platform", [None, "compass", "taobao", "xx", "tt tb"])
def test_missing_or_invalid_platform_stops_before_any_recipe(command, platform):
    """No platform-specific entrypoint may default silently to another account."""
    # 即使只展开命令，平台错误也必须立即拒绝，不能输出待执行命令。
    arguments = [command] + ([] if platform is None else [f"PLATFORM={platform}"])
    result = make_output(*arguments)
    assert result.returncode != 0
    assert "PLATFORM" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("platform,internal,config,task", [
    ("tt", "compass", "config/tasks.yaml", "compass_household_cleaning_realtime"),
    ("tb", "taobao", "config/taobao.yaml", "taobao_household_cleaning_realtime"),
])
def test_platform_resources_and_idle_gui_defaults(platform, internal, config, task):
    """默认只打开GUI，平台资源映射及手动开始后的正式模式不变。"""
    for command in ("login", "run", "status"):
        # 覆盖最终CONFIG避免依赖当前机器是否存在忽略的验收文件。
        result = make_output(command, f"PLATFORM={platform}", f"CONFIG={config}")
        assert result.returncode == 0, result.stderr
        assert f'--config "{config}"' in result.stdout
        assert f"--platform {internal}" in result.stdout
        if command == "run":
            assert f'--task "{task}"' in result.stdout
            assert "--force" in result.stdout and "DINGTALK_ENABLED=true" in result.stdout
            assert "--no-gui" not in result.stdout and "--idle" in result.stdout


@pytest.mark.parametrize("arguments,expected", [
    (["MODE=normal", "GUI=no", "NOTIFY=no"], ["--no-gui", "DINGTALK_ENABLED=false"]),
    (["MODE=dry-run", "START=no"], ["--dry-run", "--idle"]),
])
def test_run_overrides_preserve_existing_mode_semantics(arguments, expected):
    """Changing mode or notification must not silently retain force semantics."""
    # 验证参数组合的实际命令，普通模式不携带--force。
    result = make_output("run", "PLATFORM=tb", *arguments)
    assert result.returncode == 0, result.stderr
    assert "--force" not in result.stdout
    assert all(value in result.stdout for value in expected)


@pytest.mark.parametrize("command,arguments", [
    ("run", ["MODE=unknown"]), ("run", ["GUI=yes no"]),
    ("run", ["GUI=", "START=yes no"]),
    ("run", ["GUI=no", "START=no"]), ("run", ["NOTIFY=maybe"]),
    ("clean", []), ("clean", ["ACTION=all"]),
    ("check", ["ACTION=install"]),
    ("schedule", ["ACTION=data"]),
    ("start", ["MODE=unknown"]), ("start", ["NOTIFY=maybe"]),
])
def test_invalid_actions_and_options_never_expand_a_destructive_recipe(command, arguments):
    """Check cannot become install, and cleanup requires an explicit valid action."""
    # 读取Makefile阶段拒绝错误，比在shell执行时拒绝更早。
    result = make_output(command, "PLATFORM=tb", *arguments)
    assert result.returncode != 0 and not result.stdout


def test_help_install_and_selected_platform_service_surface():
    """Help, installation and dual-platform startup do not require a platform."""
    assert make_output().returncode == 0
    assert make_output("help").returncode == 0
    assert make_output("install").returncode == 0
    # help的真实执行没有副作用；按缩进解析公开命令列表。
    help_result = subprocess.run(["make", "help"], capture_output=True, text=True)
    assert {line.split()[0] for line in help_result.stdout.splitlines() if line.startswith("  ")} == {
        "help", "install", "start", *PLATFORM_COMMANDS,
    }
    # 模板脚本选择独立平台标签和配置。
    service = make_output("schedule", "PLATFORM=tb", "ACTION=check")
    assert "COLLECTOR_PLATFORM=taobao" in service.stdout and "--dry-run" in service.stdout
    for obsolete in ("app", "taobao-run", "taobao-login", "taobao-status", "service", "test", "scheduler"):
        assert make_output(obsolete, "PLATFORM=tb").returncode != 0


def test_start_expands_both_independent_platform_defaults():
    """Expand recursive run commands without starting GUI, Chrome or notifications."""
    # 各平台配置及默认任务不能被父级单个平台参数串用。
    result = make_output("start", "TT_CONFIG=config/tasks.yaml", "TB_CONFIG=config/taobao.yaml")
    assert result.returncode == 0, result.stderr
    assert '--platform compass --task "compass_household_cleaning_realtime" --force' in result.stdout
    assert '--platform taobao --task "taobao_household_cleaning_realtime" --force' in result.stdout
    assert '--config "config/tasks.yaml"' in result.stdout
    assert '--config "config/taobao.yaml"' in result.stdout
    assert result.stdout.count("DINGTALK_ENABLED=true") == 2
    assert "--no-gui" not in result.stdout and result.stdout.count("--idle") == 2


@pytest.mark.parametrize("command", ["run", "start"])
def test_explicit_start_retains_immediate_execution(command):
    """默认空闲后，显式 START=yes 仍能立即开始采集。"""
    # 只展开命令，避免测试启动真实 GUI 或浏览器。
    result = make_output(command, "PLATFORM=tb", "START=yes")
    assert result.returncode == 0, result.stderr
    assert "--idle" not in result.stdout


@pytest.mark.parametrize("failed_platform", ["none", "compass", "taobao"])
def test_start_runs_both_children_concurrently_and_preserves_failure(tmp_path, failed_platform):
    """Run the actual Make recipes with a bounded fake collector to prove overlap."""
    # 替身写入启动证据并等待另一平台；串行实现会在等待超时后失败。
    probe = tmp_path / "collector_probe.py"
    probe.write_text("""
import json, os, sys, time
from pathlib import Path
# 测试材料只位于替身旁的临时目录。
root = Path(__file__).parent
# 根据真实CLI参数区分两个替身进程。
platform = sys.argv[sys.argv.index('--platform') + 1]
# 必须在另一平台启动后才允许本平台完成。
other = 'taobao' if platform == 'compass' else 'compass'
(root / platform).write_text(json.dumps({'arguments': sys.argv, 'notify': os.environ['DINGTALK_ENABLED']}))
# 有界等待让串行错误在测试中明确失败。
deadline = time.monotonic() + 3
while not (root / other).exists():
    if time.monotonic() > deadline:
        raise SystemExit(99)
    time.sleep(0.01)
(root / (platform + '-overlap')).write_text('confirmed')
raise SystemExit(7 if os.environ['FAILED_PLATFORM'] == platform else 0)
""")
    # 仅替换Python执行器；生产start/run目标及参数传递均真实执行。
    result = subprocess.run(
        ["make", "start", f"PYTHON={sys.executable} {probe}", "MODE=dry-run", "NOTIFY=no",
         "TT_CONFIG=config/tasks.yaml", "TB_CONFIG=config/taobao.yaml"],
        env={**os.environ, "FAILED_PLATFORM": failed_platform}, capture_output=True, text=True, timeout=15,
    )
    assert (result.returncode == 0) is (failed_platform == "none"), result.stderr
    for platform in ("compass", "taobao"):
        # 两边都必须真正执行，并收到相同运行开关及各自业务平台。
        payload = json.loads((tmp_path / platform).read_text())
        assert (tmp_path / (platform + "-overlap")).exists()
        assert payload["notify"] == "false"
        assert "--dry-run" in payload["arguments"] and "--force" not in payload["arguments"]


@pytest.mark.parametrize("force,dry_run,idle", [(True, False, False), (False, False, False), (False, True, True)])
def test_gui_cli_request_and_worker_reload_keep_platform_and_mode(monkeypatch, force, dry_run, idle):
    """Test real dispatch and worker reload, replacing only external collection and UI."""
    # 用主配置检查真正的多平台过滤，不把禁用的淘宝任务隐式启用。
    config = load_config(Path("config/tasks.yaml"))
    # 参数直接来自真实解析器，生产GUI构造前截获启动请求。
    arguments = cli.build_parser().parse_args(["run", "--platform", "compass", "--task", config.tasks[0].id]
                                              + (["--force"] if force else [])
                                              + (["--dry-run"] if dry_run else [])
                                              + (["--idle"] if idle else []))
    # 记录最终浏览器执行边界，阻止访问真实账号和数据库。
    calls = []

    def capture_collection(actual_config, task_id, **options):
        """Observe the real worker's filtered config and collection options."""
        calls.append((actual_config, task_id, options))
        return 0

    def capture_gui(actual_config, request):
        """Preserve the request while executing the production worker synchronously."""
        assert {task.platform for task in actual_config.tasks} == {"compass"}
        assert request.auto_start is not idle and request.force is force and request.platform == "compass"
        assert request.dry_run is dry_run and request.lock_mode
        # 手动调用工作线程只测试重读配置，不创建窗口或QThread。
        worker = gui.CollectionWorker(request, gui.RunMode.DRY_RUN if dry_run else gui.RunMode.OFFICIAL,
                                      request.force, gui.CollectionControl())
        worker.run()
        return 0

    monkeypatch.setattr(gui, "run_collection", capture_collection)
    monkeypatch.setattr(gui, "run_gui", capture_gui)
    assert cli._dispatch_configured_command(arguments, config) == 0
    assert len(calls) == 1
    assert {task.platform for task in calls[0][0].tasks} == {"compass"}
    assert calls[0][2]["force"] is force and calls[0][2]["dry_run"] is dry_run


def test_platform_selection_rejects_foreign_task_and_filters_scheduler(monkeypatch):
    """Single-platform Make commands cannot dispatch another platform's configured task."""
    # 两个平台都声明在主配置，选择平台不能改变enabled标志。
    config = load_config(Path("config/tasks.yaml"))
    selected = config.for_platform("taobao")
    assert all(task.platform == "taobao" and not task.enabled for task in selected.tasks)
    assert selected.browser_for("taobao").profile_dir == config.browser_for("taobao").profile_dir
    # 跨平台TASK在GUI启动前拒绝。
    arguments = cli.build_parser().parse_args(["run", "--platform", "taobao", "--task", config.tasks[0].id])
    with pytest.raises(ValueError, match="does not belong"):
        cli._dispatch_configured_command(arguments, config)
    # 同一个调度入口只收到所选任务范围，不产生进程或后台服务。
    captured = []
    monkeypatch.setattr(cli, "run_scheduler", lambda selected_config: captured.append(selected_config) or 0)
    arguments = cli.build_parser().parse_args(["scheduler", "--platform", "compass"])
    assert cli._dispatch_configured_command(arguments, config) == 0
    assert len(captured) == 1 and {task.platform for task in captured[0].tasks} == {"compass"}


@pytest.mark.parametrize("force", [True, False])
def test_gui_start_collection_does_not_override_explicit_normal_mode(monkeypatch, force):
    """Exercise the real GUI start method without launching a Qt thread or Chrome."""
    # 信号和线程替身只接收连接；真正的GUI方法必须保留请求中的force值。
    signal = SimpleNamespace(connect=lambda *args: None, emit=lambda *args: None)
    # start记录只是验证自动执行入口，不执行采集或浏览器任务。
    starts = []
    thread = SimpleNamespace(started=signal, finished=signal, quit=lambda: None,
                             deleteLater=lambda: None, start=lambda: starts.append(True))

    def worker(request, mode, actual_force, control):
        """Capture the mode passed by the production GUI start method."""
        return SimpleNamespace(force=actual_force, event_received=signal, finished=signal,
                               moveToThread=lambda *args: None, run=lambda: None, deleteLater=lambda: None)

    # 最小窗口状态覆盖真实方法读取的全部UI边界，避免引入屏幕或Qt平台依赖。
    window = SimpleNamespace(collecting=False, request=SimpleNamespace(force=force, lock_mode=True),
                             _selected_mode=lambda: gui.RunMode.OFFICIAL,
                             handle_event=lambda *args: None, _collection_finished=lambda *args: None,
                             run_status_label=SimpleNamespace(setText=lambda *args: None),
                             notification_label=SimpleNamespace(setText=lambda *args: None), current_csv_path=None,
                             _apply_progress_state=lambda: None, _update_action_states=lambda: None)
    monkeypatch.setattr(gui, "CollectionWorker", worker)
    monkeypatch.setattr(gui, "QThread", lambda *args: thread)
    gui.CollectorWindow.start_collection(window)
    assert window.collection_worker.force is force
    assert starts == [True] and window.collecting
