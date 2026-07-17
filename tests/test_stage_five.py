"""Restricted stage-five launchd and delivery artifact tests."""

import os
import plistlib
import subprocess
from pathlib import Path


# LaunchAgent 模板是守护行为的唯一仓库内契约。
PLIST_TEMPLATE = Path(
    "launchd/com.zhuanz1.douyin-compass-collector.plist.template"
)
# 三个脚本覆盖安装、卸载和只读状态查询。
SCRIPT_PATHS = (
    Path("scripts/install_launchd.sh"),
    Path("scripts/uninstall_launchd.sh"),
    Path("scripts/status_launchd.sh"),
)


def test_launchd_template_uses_frozen_foreground_scheduler_without_secrets() -> None:
    """Keep launchd deterministic, user-scoped, and free of credentials."""

    # plistlib 能解析占位符模板，证明 XML 结构本身有效。
    payload = plistlib.loads(PLIST_TEMPLATE.read_bytes())
    # ProgramArguments 用于核对后台不会更新依赖或执行其他命令。
    arguments = payload["ProgramArguments"]
    # 模板文本用于扫描明确禁止的认证和通知字段。
    template_text = PLIST_TEMPLATE.read_text(encoding="utf-8").lower()

    assert payload["Label"] == "com.zhuanz1.douyin-compass-collector"
    assert arguments[1:] == [
        "run",
        "--frozen",
        "python",
        "-m",
        "compass_collector",
        "scheduler",
        "--config",
        "__CONFIG_PATH__",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["StandardOutPath"] == "/dev/null"
    assert payload["StandardErrorPath"] == "/dev/null"
    assert not any(
        marker in template_text
        for marker in ("sessionid", "mstoken", "webhook", "secret", "cookie")
    )


def test_launchd_scripts_are_executable_and_parse_as_bash() -> None:
    """Validate every delivery script without invoking launchctl."""

    for script_path in SCRIPT_PATHS:
        # 可执行位是 README 命令能够直接运行的前提。
        assert os.access(script_path, os.X_OK)
        # bash -n 只解析语法，不执行脚本内容。
        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_install_dry_run_never_writes_user_launchagents(tmp_path: Path) -> None:
    """Render and lint the real plist while leaving launchd untouched."""

    # 临时 HOME 让任何意外用户目录写入都落在 pytest 隔离区。
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    # dry-run 允许 PlistBuddy 和 plutil，但明确跳过 install 与 launchctl。
    result = subprocess.run(
        [str(SCRIPT_PATHS[0]), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    # 目标 plist 若出现即表示受限阶段边界被破坏。
    target_path = (
        tmp_path
        / "Library"
        / "LaunchAgents"
        / "com.zhuanz1.douyin-compass-collector.plist"
    )

    assert result.returncode == 0, result.stderr
    assert "未调用 launchctl" in result.stdout
    assert not target_path.exists()


def test_delivery_documents_cover_operations_and_security() -> None:
    """Require the handoff docs needed to operate the project on another Mac."""

    # 交付文档均由仓库直接版本管理。
    readme = Path("README.md").read_text(encoding="utf-8")
    backup_guide = Path("docs/备份与恢复.md").read_text(encoding="utf-8")
    troubleshooting_guide = Path("docs/故障处理.md").read_text(encoding="utf-8")

    assert "install_launchd.sh --dry-run" in readme
    assert "新 Mac 交付检查清单" in readme
    assert "不要迁移旧 Chrome Profile" in backup_guide
    assert "auth_required" in troubleshooting_guide
    assert "missed" in troubleshooting_guide


def test_makefile_exposes_compact_parameterized_commands() -> None:
    """Keep the common workflow short without duplicating run and service targets."""

    # Makefile 文本用于锁定公开命令名称和默认安全边界。
    makefile = Path("Makefile").read_text(encoding="utf-8")
    # 常用命令覆盖安装、采集、调度、测试和参数化 LaunchAgent 管理。
    expected_targets = {
        "help",
        "install",
        "login",
        "run",
        "app",
        "notify-test",
        "clear-data",
        "status",
        "scheduler",
        "test",
        "check",
        "service",
    }

    assert all(f"{target}:" in makefile for target in expected_targets)
    assert "dry-run:" not in makefile
    assert "force:" not in makefile
    assert "launchd-check:" not in makefile
    assert "MODE ?= normal" in makefile
    assert "GUI ?= yes" in makefile
    assert "ACTION ?= check" in makefile
    assert "--frozen" in makefile
    assert "TASK ?= product_hot_sale_all_level3" in makefile
