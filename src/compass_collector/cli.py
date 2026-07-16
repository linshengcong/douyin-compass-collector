"""Command-line interface for login, collection, status, and scheduling."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from compass_collector.config import load_config
from compass_collector.runner import run_collection, run_login, run_status
from compass_collector.scheduler import run_scheduler


# 默认配置路径与工程方案保持一致。
DEFAULT_CONFIG_PATH = Path("config/tasks.yaml")


def build_parser() -> argparse.ArgumentParser:
    """Build the currently authorized stage-one through stage-four commands."""

    # 顶层解析器只提供当前已授权阶段的命令。
    parser = argparse.ArgumentParser(prog="python -m compass_collector")
    # 子命令必填，避免无意启动 Chrome。
    subparsers = parser.add_subparsers(dest="command", required=True)

    # login 命令用于人工维护持久化登录态。
    login_parser = subparsers.add_parser("login", help="open the persistent Chrome profile")
    login_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    # run 命令执行全部启用任务或一个指定任务。
    run_parser = subparsers.add_parser(
        "run", help="collect and publish a ranking snapshot"
    )
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser.add_argument("--task", dest="task_id")
    # --force 和 --dry-run 语义冲突，同一次命令只允许选择一个。
    run_mode = run_parser.add_mutually_exclusive_group()
    run_mode.add_argument("--force", action="store_true")
    run_mode.add_argument("--dry-run", action="store_true")

    # status 命令只读取最近 run 摘要，不启动 Chrome。
    status_parser = subparsers.add_parser("status", help="show recent task runs")
    status_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    status_parser.add_argument("--limit", type=int, default=20)

    # scheduler 命令以前台常驻方式执行，不实现 launchd 守护。
    scheduler_parser = subparsers.add_parser(
        "scheduler", help="run the foreground APScheduler"
    )
    scheduler_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser


def main() -> None:
    """Validate configuration and dispatch one authorized command."""

    # CLI 参数在任何配置或浏览器操作前解析。
    arguments = build_parser().parse_args()
    try:
        # 严格配置在启动 Chrome 前完成全量校验。
        config = load_config(arguments.config)
        if arguments.command == "login":
            exit_code = run_login(config)
        elif arguments.command == "run":
            exit_code = run_collection(
                config,
                arguments.task_id,
                force=arguments.force,
                dry_run=arguments.dry_run,
            )
        elif arguments.command == "status":
            if arguments.limit <= 0:
                raise ValueError("status --limit must be positive")
            exit_code = run_status(config, arguments.limit)
        else:
            exit_code = run_scheduler(config)
    except (OSError, ValueError, ValidationError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        exit_code = 2
    except Exception as error:
        # 未预期浏览器错误只输出类型，避免底层异常夹带 URL 或请求上下文。
        print(f"运行失败：{type(error).__name__}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
