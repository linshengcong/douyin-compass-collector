"""Build portable macOS and Windows desktop archives with PyInstaller."""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# 打包后对外展示的应用名称与 ZIP 根目录名称保持一致。
APPLICATION_NAME = "抖音罗盘采集器"
# PyInstaller 在不同系统中使用不同的数据源分隔符。
DATA_SEPARATOR = ";" if platform.system() == "Windows" else ":"


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit packaging command parser used by CI and local release work."""

    parser = argparse.ArgumentParser(description="构建抖音罗盘采集器桌面 ZIP")
    parser.add_argument("--target", choices=("macos-arm64", "windows-x64"), required=True)
    return parser


def add_data_argument(source: Path, destination: str) -> str:
    """Format one PyInstaller data argument for the active host platform."""

    return f"{source}{DATA_SEPARATOR}{destination}"


def assert_host_matches_target(target: str) -> None:
    """Reject cross-platform builds because PyInstaller builds on the host platform."""

    current_system = platform.system()
    expected_system = "Darwin" if target == "macos-arm64" else "Windows"
    if current_system != expected_system:
        raise SystemExit(f"target {target} must run on {expected_system}, got {current_system}")


def build_application(project_root: Path, target: str) -> Path:
    """Run PyInstaller and return the platform-specific app or executable directory."""

    icon_path = project_root / "assets" / "icons" / (
        "douyin-compass.icns" if target == "macos-arm64" else "douyin-compass.ico"
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--optimize",
        "2",
        "--name",
        APPLICATION_NAME,
        "--icon",
        str(icon_path),
        "--add-data",
        add_data_argument(project_root / "migrations", "migrations"),
        "--add-data",
        add_data_argument(project_root / "alembic.ini", "."),
        "--add-data",
        add_data_argument(project_root / ".env.example", "."),
        "--add-data",
        add_data_argument(project_root / "config", "config"),
        "--paths",
        str(project_root / "src"),
        # GUI 和迁移链路含有延迟导入，显式收集避免桌面版运行时缺少模块。
        "--collect-submodules",
        "compass_collector",
        # Alembic loads this standard-library submodule from migration resources at runtime.
        "--hidden-import",
        "logging.config",
        "--distpath",
        str(project_root / "dist"),
        "--workpath",
        str(project_root / "build"),
        "--specpath",
        str(project_root / "build"),
        # __main__ preserves the normal module-entry dispatch when bundled.
        str(project_root / "src" / "compass_collector" / "__main__.py"),
    ]
    if target == "macos-arm64":
        command.extend(["--target-architecture", "arm64", "--osx-bundle-identifier", "com.zhuanz1.douyin-compass-collector"])
    # subprocess 继承 CI 输出，构建失败时可以直接定位缺失的系统依赖。
    subprocess.run(command, check=True, cwd=project_root)
    return project_root / "dist" / (
        f"{APPLICATION_NAME}.app" if target == "macos-arm64" else APPLICATION_NAME
    )


def assemble_archive(project_root: Path, application_path: Path, target: str) -> Path:
    """Create one self-contained ZIP while deliberately excluding private runtime data."""

    release_root = project_root / "release" / f"{APPLICATION_NAME}-{target}"
    if release_root.exists():
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True)
    # macOS app bundles and Windows one-folder payloads both retain PyInstaller layout.
    shutil.copytree(application_path, release_root / application_path.name)
    # 任务配置已经随应用写入 Resources，发布根目录无需再暴露一个可编辑副本。
    shutil.copy2(project_root / "使用说明.md", release_root / "使用说明.md")
    archive_path = project_root / "release" / f"{APPLICATION_NAME}-{target}.zip"
    if archive_path.exists():
        archive_path.unlink()
    # make_archive 只压缩 release 根目录内容，用户解压后即可直接启动。
    shutil.make_archive(
        str(archive_path.with_suffix("")),
        "zip",
        root_dir=release_root.parent,
        base_dir=release_root.name,
    )
    return archive_path


def main() -> None:
    """Build one host-native portable desktop archive."""

    target = build_parser().parse_args().target
    assert_host_matches_target(target)
    project_root = Path(__file__).resolve().parents[1]
    application_path = build_application(project_root, target)
    assemble_archive(project_root, application_path, target)
    # Windows GitHub Runner 的默认控制台编码可能不支持中文文件名。
    # 这里保持 ASCII 日志，避免 ZIP 已生成却因最后一条提示输出失败。
    print("Desktop archive created successfully.")


if __name__ == "__main__":
    main()
