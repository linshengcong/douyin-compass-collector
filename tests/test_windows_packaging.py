"""Windows frozen-application regression tests without a Windows host."""

import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, reset_tzpath

import pytest

from compass_collector import cli as cli_module
from compass_collector import gui as gui_module
from compass_collector.runtime_logging import EVENT_STREAM_PATH_ENV, RuntimeLogger


def test_iana_timezone_loads_without_system_timezone_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require the bundled tzdata fallback used by Windows frozen applications."""

    # 清空已缓存时区，避免测试进程此前的 macOS 系统数据掩盖依赖缺失。
    ZoneInfo.clear_cache()
    # 空 TZPATH 模拟 Windows 没有 IANA 系统时区数据库。
    monkeypatch.setenv("PYTHONTZPATH", "")
    reset_tzpath()
    try:
        assert ZoneInfo("Asia/Shanghai").key == "Asia/Shanghai"
    finally:
        # 恢复解释器默认时区搜索路径，避免影响同一 pytest 进程的后续测试。
        monkeypatch.delenv("PYTHONTZPATH", raising=False)
        reset_tzpath()
        ZoneInfo.clear_cache()


def test_windowed_scheduler_events_are_written_to_the_instance_event_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep GUI Scheduler state observable when a frozen child has no stdout."""

    # event_path 模拟 GUI 为本次 Scheduler 子进程提供的专用事件文件。
    event_path = tmp_path / "scheduler.events.jsonl"
    # windowed PyInstaller 程序没有可写标准输出。
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setenv(EVENT_STREAM_PATH_ENV, str(event_path))

    RuntimeLogger(tmp_path / "logs").emit(
        level="INFO",
        event="scheduler_started",
        message="Scheduler 已启动",
        stage="scheduling",
    )

    # GUI 文件轮询依赖完整 JSONL 行，而非窗口程序中不可用的 stdout。
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["event"] == "scheduler_started"


def test_gui_reads_appended_scheduler_event_file(tmp_path: Path) -> None:
    """Feed the same event-file protocol into the GUI polling seam."""

    # event_path 模拟 windowed Scheduler 已完整写入的一条安全事件。
    event_path = tmp_path / "scheduler.events.jsonl"
    event_path.write_text(
        json.dumps({"event": "scheduled_group_started"}) + "\n",
        encoding="utf-8",
    )

    events, offset, buffered_text = gui_module.read_scheduler_event_file(
        event_path,
        0,
        "",
    )

    assert events == [{"event": "scheduled_group_started"}]
    assert offset == event_path.stat().st_size
    assert buffered_text == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["login"],
        ["run", "--no-gui"],
    ],
)
def test_packaged_app_rejects_stdin_dependent_console_commands(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid entering input() from a PyInstaller windowed executable."""

    # parser 保持用户命令的真实参数形状。
    parsed_arguments = cli_module.build_parser().parse_args(arguments)
    # 打包环境和可见提示替身共同隔离 Qt 与真实浏览器。
    monkeypatch.setattr(cli_module, "is_packaged_application", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_show_packaged_console_command_notice",
        lambda: 2,
    )

    assert cli_module._dispatch_configured_command(parsed_arguments, object()) == 2


def test_windows_package_explicitly_collects_tzdata() -> None:
    """Keep PyInstaller data collection aligned with the runtime dependency."""

    # 打包脚本是冻结资源的唯一声明边界。
    package_script = Path("scripts/package_pyinstaller.py").read_text(encoding="utf-8")

    assert '"--collect-data"' in package_script
    assert '"tzdata"' in package_script
