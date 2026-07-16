"""Command-line interface for stage-one login and collection."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from compass_collector.config import load_config
from compass_collector.runner import run_collection, run_login


# 默认配置路径与工程方案保持一致。
DEFAULT_CONFIG_PATH = Path("config/tasks.yaml")


def build_parser() -> argparse.ArgumentParser:
    """Build the two-command stage-one CLI parser."""

    # 顶层解析器只提供阶段一已授权命令。
    parser = argparse.ArgumentParser(prog="python -m compass_collector")
    # 子命令必填，避免无意启动 Chrome。
    subparsers = parser.add_subparsers(dest="command", required=True)

    # login 命令用于人工维护持久化登录态。
    login_parser = subparsers.add_parser("login", help="open the persistent Chrome profile")
    login_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    # run 命令执行全部启用任务或一个指定任务。
    run_parser = subparsers.add_parser("run", help="collect raw ranking responses")
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser.add_argument("--task", dest="task_id")
    return parser


def main() -> None:
    """Validate configuration and dispatch a stage-one command."""

    # CLI 参数在任何配置或浏览器操作前解析。
    arguments = build_parser().parse_args()
    try:
        # 严格配置在启动 Chrome 前完成全量校验。
        config = load_config(arguments.config)
        if arguments.command == "login":
            exit_code = run_login(config)
        else:
            exit_code = run_collection(config, arguments.task_id)
    except (OSError, ValueError, ValidationError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        exit_code = 2
    except Exception as error:
        # 未预期浏览器错误只输出类型，避免底层异常夹带 URL 或请求上下文。
        print(f"运行失败：{type(error).__name__}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
