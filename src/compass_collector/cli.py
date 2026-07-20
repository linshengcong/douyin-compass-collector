"""Command-line interface for login, collection, status, and scheduling."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from compass_collector.config import AppConfig, load_config
from compass_collector.local_data import clear_local_data_with_locks
from compass_collector.notifier import load_project_environment, run_notification_test
from compass_collector.runner import run_collection, run_login, run_status
from compass_collector.runtime_locks import RuntimeLockBusy
from compass_collector.runtime_logging import RuntimeLogger
from compass_collector.scheduler import run_scheduler
from compass_collector.app_paths import (
    default_config_path,
    dotenv_path,
    ensure_portable_data_root,
    is_packaged_application,
    runtime_root,
)


# 默认配置路径与工程方案保持一致。
DEFAULT_CONFIG_PATH = default_config_path()
# CLI 级通知测试复用现有安全日志目录。
RUNTIME_LOG_DIRECTORY = runtime_root() / "logs"


def build_parser() -> argparse.ArgumentParser:
    """Build all currently supported local collector commands."""

    # 顶层解析器只提供当前已授权阶段的命令。
    parser = argparse.ArgumentParser(prog="python -m compass_collector")
    # 子命令必填，避免无意启动 Chrome。
    subparsers = parser.add_subparsers(dest="command", required=True)

    # login 命令用于人工维护持久化登录态。
    login_parser = subparsers.add_parser("login", help="open the persistent Chrome profile")
    login_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    # app 命令只打开空闲 GUI 控制台，不自动启动采集。
    app_parser = subparsers.add_parser("app", help="open the idle desktop console")
    app_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    app_parser.add_argument("--task", dest="task_id")

    # notify-test 显式发送一条真实测试消息，不需要加载业务任务配置。
    subparsers.add_parser("notify-test", help="send one DingTalk test message")

    # clear-data 是开发期破坏性操作，必须显式提供 --yes。
    clear_data_parser = subparsers.add_parser(
        "clear-data",
        help="clear local collection data but preserve the Chrome profile",
    )
    clear_data_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    clear_data_parser.add_argument("--yes", action="store_true")

    # run 命令默认使用 GUI，--no-gui 显式回退终端模式。
    run_parser = subparsers.add_parser(
        "run", help="collect and publish a ranking snapshot"
    )
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser.add_argument("--task", dest="task_id")
    run_parser.add_argument("--no-gui", action="store_true")
    # --force 和 --dry-run 语义冲突，同一次命令只允许选择一个。
    run_mode = run_parser.add_mutually_exclusive_group()
    run_mode.add_argument("--force", action="store_true")
    run_mode.add_argument("--dry-run", action="store_true")

    # status 命令只读取最近批次摘要，不启动 Chrome。
    status_parser = subparsers.add_parser("status", help="show recent task runs")
    status_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    status_parser.add_argument("--limit", type=int, default=20)

    # scheduler 命令以前台常驻方式执行，不实现 launchd 守护。
    scheduler_parser = subparsers.add_parser(
        "scheduler", help="run the foreground APScheduler"
    )
    scheduler_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    # _smoke 只供打包 CI 验证冻结资源，不作为用户可用命令展示。
    subparsers.add_parser("_smoke", help=argparse.SUPPRESS)
    return parser


def main() -> None:
    """Validate configuration and dispatch one authorized command."""

    # CLI 参数在任何配置或浏览器操作前解析。
    cli_arguments = sys.argv[1:]
    # 双击打包后的桌面应用时默认进入空闲 GUI，而开发 CLI 保持显式命令。
    if is_packaged_application() and not cli_arguments:
        cli_arguments = ["app"]
    arguments = build_parser().parse_args(cli_arguments)
    try:
        if arguments.command == "_smoke":
            exit_code = run_packaged_smoke_test()
        elif is_packaged_application():
            _validate_portable_environment()
            # .env 只补充当前进程缺失值，系统环境变量保持最高优先级。
            load_project_environment()
            if arguments.command == "notify-test":
                exit_code = run_notification_test(RuntimeLogger(RUNTIME_LOG_DIRECTORY))
            else:
                # 严格业务配置在启动 Chrome 前完成全量校验。
                config = load_config(arguments.config)
                exit_code = _dispatch_configured_command(arguments, config)
        else:
            # .env 只补充当前进程缺失值，系统环境变量保持最高优先级。
            load_project_environment()
            if arguments.command == "notify-test":
                exit_code = run_notification_test(RuntimeLogger(RUNTIME_LOG_DIRECTORY))
            else:
                # 严格业务配置在启动 Chrome 前完成全量校验。
                config = load_config(arguments.config)
                exit_code = _dispatch_configured_command(arguments, config)
    except RuntimeLockBusy as error:
        print(f"启动失败：{error.role} 已被其他进程占用", file=sys.stderr)
        exit_code = 2
    except (OSError, ValueError, ValidationError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        exit_code = 2
    except ModuleNotFoundError as error:
        # 模块名不包含账号或请求数据，可用于定位打包漏收集的依赖。
        missing_module = error.name or "unknown"
        print(f"运行失败：缺少打包组件 {missing_module}", file=sys.stderr)
        exit_code = 1
    except Exception as error:
        # 未预期浏览器错误只输出类型，避免底层异常夹带 URL 或请求上下文。
        print(f"运行失败：{type(error).__name__}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


def _dispatch_configured_command(
    arguments: argparse.Namespace,
    config: AppConfig,
) -> int:
    """Dispatch one command that already has a validated business config."""

    # windowed 桌面包没有 stdin，不能进入依赖 Enter 的终端工作流。
    if is_packaged_application() and (
        arguments.command == "login"
        or (arguments.command == "run" and arguments.no_gui)
    ):
        return _show_packaged_console_command_notice()
    if arguments.command == "login":
        exit_code = run_login(config)
    elif arguments.command == "app":
        # PySide6 延迟导入，status、login 和后台 Scheduler 不初始化 Qt。
        from compass_collector.gui import GuiLaunchRequest, run_gui

        exit_code = run_gui(
            config,
            GuiLaunchRequest(
                config_path=arguments.config,
                task_id=arguments.task_id,
                auto_start=False,
            ),
        )
    elif arguments.command == "run":
        if arguments.no_gui:
            exit_code = run_collection(
                config,
                arguments.task_id,
                force=arguments.force,
                dry_run=arguments.dry_run,
            )
        else:
            # GUI 命令锁定 run/dry-run/force 的启动语义并自动执行。
            from compass_collector.gui import GuiLaunchRequest, run_gui

            exit_code = run_gui(
                config,
                GuiLaunchRequest(
                    config_path=arguments.config,
                    task_id=arguments.task_id,
                    auto_start=True,
                    dry_run=arguments.dry_run,
                    force=arguments.force,
                    lock_mode=True,
                ),
            )
    elif arguments.command == "clear-data":
        if not arguments.yes:
            raise ValueError("clear-data requires --yes")
        # 只在同时获得 Scheduler 和采集锁后执行白名单删除。
        cleanup_summary = clear_local_data_with_locks(
            runtime_root(),
            config.database.path,
        )
        print(
            "本地采集数据清理完成："
            f"数据库文件 {cleanup_summary.database_files}，"
            f"数据目录 {cleanup_summary.runtime_directories}，"
            f"失败 {cleanup_summary.failures}"
        )
        exit_code = 0 if cleanup_summary.succeeded else 1
    elif arguments.command == "status":
        if arguments.limit <= 0:
            raise ValueError("status --limit must be positive")
        exit_code = run_status(config, arguments.limit)
    else:
        exit_code = run_scheduler(config)
    return exit_code


def run_packaged_smoke_test() -> int:
    """Start frozen Playwright resources and emit one event for packaging CI."""

    # RuntimeLogger 同时验证 Windows tzdata 和 windowed 事件文件传输。
    runtime_logger = RuntimeLogger(RUNTIME_LOG_DIRECTORY)
    # Playwright driver 在启动时验证 PyInstaller 已收集 Node 与 driver 资源。
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        runtime_logger.emit(
            level="INFO",
            event="packaged_smoke_succeeded",
            message="打包运行时资源验证完成",
            stage="packaging",
        )
    finally:
        playwright.stop()
    return 0


def _show_packaged_console_command_notice() -> int:
    """Explain that the windowed desktop package cannot run stdin workflows."""

    # 延迟导入避免开发期非 GUI CLI 命令初始化 Qt。
    from PySide6.QtWidgets import QApplication, QMessageBox

    application = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.information(
        None,
        "请使用图形界面",
        "桌面版不支持需要终端输入的 login 或 --no-gui 命令。\n"
        "请直接启动采集器，在窗口中完成登录和采集。",
    )
    # application 保持局部强引用直到模态提示框关闭。
    del application
    return 2


def _validate_portable_environment() -> None:
    """Guide desktop users to the external dotenv folder before a real run starts."""

    # 先创建目录，用户无需在 Finder 或 Explorer 中手动显示隐藏文件。
    data_root = ensure_portable_data_root()
    if dotenv_path().is_file():
        return
    # PySide6 只在打包 GUI 缺配置时导入，终端开发命令不受影响。
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QMessageBox

    application = QApplication.instance() or QApplication(sys.argv)
    message_box = QMessageBox()
    message_box.setIcon(QMessageBox.Warning)
    message_box.setWindowTitle("缺少配置文件")
    message_box.setText("未找到应用数据目录中的配置.env，暂时无法启动采集器。")
    message_box.setInformativeText(
        "请将可信来源提供的 .env 重命名为“配置.env”，放入已打开的应用数据文件夹后，再重新启动应用。"
    )
    open_button = message_box.addButton("打开配置目录", QMessageBox.AcceptRole)
    message_box.addButton("退出", QMessageBox.RejectRole)
    message_box.exec()
    if message_box.clickedButton() is open_button:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_root)))
    # 没有 .env 时不能误以为账号与通知配置已经生效。
    raise ValueError("portable_env_missing")
