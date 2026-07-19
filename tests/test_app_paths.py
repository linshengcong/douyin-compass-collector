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


def test_windows_bundle_keeps_config_and_data_inside_one_folder_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Keep config and writable data in the packaged Windows application payload."""

    executable_path = tmp_path / "抖音罗盘采集器.exe"
    resource_path = tmp_path / "_internal"
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "executable", str(executable_path))
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(resource_path), raising=False)

    assert app_paths.application_root() == tmp_path
    assert app_paths.default_config_path() == resource_path / "config" / "tasks.yaml"
    assert app_paths.dotenv_path() == resource_path / "采集器数据" / "配置.env"
    assert app_paths.runtime_root() == resource_path / "采集器数据" / "runtime"


def test_macos_bundle_keeps_data_inside_app_resources(monkeypatch, tmp_path: Path) -> None:
    """Keep visible data inside the macOS app bundle for self-contained sharing."""

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

    assert app_paths.application_root() == tmp_path
    assert app_paths.portable_data_root() == resource_path / "采集器数据"


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
