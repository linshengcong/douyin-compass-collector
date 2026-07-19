"""Resolve development and portable desktop paths from one trusted place."""

import sys
from pathlib import Path


# 便携版目录和配置文件均使用可见中文名，方便通过 Finder 和 Explorer 操作。
PORTABLE_DATA_DIRECTORY_NAME = "采集器数据"
PORTABLE_ENVIRONMENT_FILENAME = "配置.env"


def is_packaged_application() -> bool:
    """Return whether the process is running from a PyInstaller bundle."""

    # PyInstaller sets sys.frozen for both macOS app bundles and Windows executables.
    return bool(getattr(sys, "frozen", False))


def application_root() -> Path:
    """Return the portable program folder that contains a bundled application."""

    if not is_packaged_application():
        # This module lives in src/compass_collector below the repository root.
        return Path(__file__).resolve().parents[2]
    executable_path = Path(sys.executable).resolve()
    # A macOS executable is nested in <root>/<app>.app/Contents/MacOS/.
    for parent in executable_path.parents:
        if parent.suffix == ".app":
            return parent.parent
    # A Windows one-folder executable is directly inside the portable folder.
    return executable_path.parent


def resource_root() -> Path:
    """Return PyInstaller bundled resources or the repository root in development."""

    if not is_packaged_application():
        return application_root()
    executable_path = Path(sys.executable).resolve()
    # macOS PyInstaller runs Python from Contents/Frameworks, while added data lives in Resources.
    for parent in executable_path.parents:
        if parent.suffix == ".app":
            return parent / "Contents" / "Resources"
    # _MEIPASS is PyInstaller's stable directory for one-folder bundled data.
    bundled_root = getattr(sys, "_MEIPASS", None)
    return Path(bundled_root) if bundled_root else executable_path.parent


def portable_data_root() -> Path:
    """Return the self-contained writable data directory inside the application."""

    # 资源根目录位于 macOS .app/Contents/Resources 或 Windows one-folder payload。
    return resource_root() / PORTABLE_DATA_DIRECTORY_NAME


def runtime_root() -> Path:
    """Return the active runtime directory without changing development defaults."""

    return portable_data_root() / "runtime" if is_packaged_application() else Path("runtime")


def default_config_path() -> Path:
    """Return the checked-in config in development or the packaged internal config."""

    return (
        resource_root() / "config" / "tasks.yaml"
        if is_packaged_application()
        else Path("config/tasks.yaml")
    )


def dotenv_path() -> Path:
    """Return the external portable dotenv path or the development dotenv path."""

    return (
        portable_data_root() / PORTABLE_ENVIRONMENT_FILENAME
        if is_packaged_application()
        else Path(".env")
    )


def ensure_portable_data_root() -> Path:
    """Create and return the distributed application's persistent data directory."""

    data_root = portable_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    return data_root


def scheduler_process_command(config_path: Path) -> tuple[str, list[str]]:
    """Build a Scheduler child-process command for development or PyInstaller."""

    if is_packaged_application():
        # Frozen executables dispatch their CLI command directly instead of Python -m.
        return str(Path(sys.executable).resolve()), ["scheduler", "--config", str(config_path)]
    return sys.executable, [
        "-m",
        "compass_collector",
        "scheduler",
        "--config",
        str(config_path),
    ]
