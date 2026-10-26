"""Freeze collector writes, preserve history and switch all configured local stores."""

import argparse
import json
import os
import shutil
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values, set_key
from sqlalchemy.engine import URL

from compass_collector.local_migration import copy_to_sqlite
from compass_collector.runtime_locks import ProcessLock


def main():
    """Require an explicit switch flag and refuse active GUI, scheduler or collection."""
    # 默认只说明入口，不在无参数时改变真实配置。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    args = parser.parse_args()
    # 记录本次迁移的唯一目录，永不覆盖以前的数据库或备份。
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    runtime = args.runtime_root.resolve()
    backup = runtime / "backups" / ("sqlite-cutover-" + stamp)
    destination = runtime / "local" / stamp
    # 连接变量沿用既有配置契约；不同平台始终使用独立文件。
    platforms = {"compass": ("compass", "COMPASS_DATABASE_URL"), "taobao": ("taobao", "TAOBAO_DATABASE_URL"), "taobao_poc": ("taobao", "TAOBAO_POC_DATABASE_URL")}
    with ExitStack() as locks:
        for platform in ("compass", "taobao"):
            for role in ("gui", "scheduler", "collection"):
                locks.enter_context(ProcessLock(runtime / "locks" / platform / (role + ".lock"), "sqlite_migration"))
        # 拒绝环境覆盖，确保切换 .env 后新进程实际使用本地文件。
        for _, key in platforms.values():
            if os.environ.get(key):
                raise ValueError("clear process override before migration: " + key)
        backup.mkdir(parents=True, mode=0o700)
        destination.mkdir(parents=True, mode=0o700)
        # 备份完整原配置，权限限制为当前用户可读写。
        original = args.env_file.read_bytes()
        shutil.copyfile(args.env_file, backup / "source.env")
        (backup / "source.env").chmod(0o600)
        values = dotenv_values(args.env_file)
        # 验证清单不包含凭证，允许与备份一起留存审计。
        report = {"created_at": stamp, "switched": False, "databases": {}}
        for name, (platform, key) in platforms.items():
            report["databases"][name] = copy_to_sqlite(values[key], destination / (name + ".sqlite3"), platform=platform, allow_empty=name == "taobao_poc")
            (backup / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        # 若用户或另一任务同时改过 .env，则保留迁移结果但不覆盖其修改。
        if args.env_file.read_bytes() != original:
            raise RuntimeError("environment changed during migration; refusing to replace it")
        # 全部核对通过后才原子替换三个数据库连接。
        staged = backup / "local.env"
        shutil.copyfile(args.env_file, staged)
        staged.chmod(0o600)
        for name, (_, key) in platforms.items():
            set_key(str(staged), key, URL.create("sqlite+pysqlite", database=str(destination / (name + ".sqlite3"))).render_as_string(hide_password=False))
        os.replace(staged, args.env_file)
        report["switched"] = True
        (backup / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("SQLite cutover verified: " + str(backup / "verification.json"), flush=True)


if __name__ == "__main__":
    main()
