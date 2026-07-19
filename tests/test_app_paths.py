"""Portable PyInstaller path and command contracts."""

from pathlib import Path

from compass_collector import app_paths
from compass_collector import config as config_module


def test_development_paths_keep_existing_relative_runtime_layout(
    monkeypatch,
) -> None:
    """Keep source-tree commands and legacy tests on their existing relative paths."""

    monkeypatch.setattr(app_paths.sys, "frozen", False, raising=False)

    assert app_paths.runtime_root() == Path("runtime")
    assert app_paths.default_config_path() == Path("config/tasks.yaml")
    assert app_paths.dotenv_path() == Path(".env")


def test_windows_bundle_keeps_config_in_bundle_and_data_in_local_app_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Keep immutable config in the bundle and writable data outside it on Windows."""

    executable_path = tmp_path / "抖音罗盘采集器.exe"
    resource_path = tmp_path / "_internal"
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "executable", str(executable_path))
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(resource_path), raising=False)
    monkeypatch.setattr(app_paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    assert app_paths.application_root() == tmp_path
    assert app_paths.default_config_path() == resource_path / "config" / "tasks.yaml"
    data_path = tmp_path / "AppData" / "Local" / "抖音罗盘采集器"
    assert app_paths.dotenv_path() == data_path / "配置.env"
    assert app_paths.runtime_root() == data_path / "runtime"


def test_macos_bundle_keeps_data_outside_signed_app_resources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Keep writable data outside the signed macOS application bundle."""

    executable_path = (
        tmp_path
        / "抖音罗盘采集器.app"
        / "Contents"
        / "MacOS"
        / "抖音罗盘采集器"
    )
    resource_path = tmp_path / "抖音罗盘采集器.app" / "Contents" / "Resources"
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "executable", str(executable_path))
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(resource_path), raising=False)
    monkeypatch.setattr(app_paths.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert app_paths.application_root() == tmp_path
    assert app_paths.portable_data_root() == (
        tmp_path / "home" / "Library" / "Application Support" / "抖音罗盘采集器"
    )


def test_packaged_scheduler_reinvokes_same_executable(monkeypatch, tmp_path: Path) -> None:
    """Avoid unsupported Python -m arguments when the GUI owns Scheduler."""

    executable_path = tmp_path / "抖音罗盘采集器.exe"
    config_path = tmp_path / "config" / "tasks.yaml"
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "executable", str(executable_path))

    program, arguments = app_paths.scheduler_process_command(config_path)

    assert program == str(executable_path)
    assert arguments == ["scheduler", "--config", str(config_path)]


def test_packaged_config_moves_runtime_paths_outside_the_application(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Map checked-in runtime paths to the portable data directory only in bundles."""

    config_path = Path("config/tasks.yaml")
    portable_runtime = tmp_path / "采集器数据" / "runtime"
    monkeypatch.setattr(config_module, "is_packaged_application", lambda: True)
    monkeypatch.setattr(config_module, "runtime_root", lambda: portable_runtime)

    config = config_module.load_config(config_path)

    assert config.browser.profile_dir == portable_runtime / "browser-profile"
    assert config.database.path == portable_runtime / "data" / "collector.db"
