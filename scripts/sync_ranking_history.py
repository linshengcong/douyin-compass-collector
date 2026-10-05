"""从本机采集库补同步所有已发布历史批次，支持断点重试和只读预检。"""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from compass_collector.ranking_sync import build_sync_payload, sync_published_index


def main() -> None:
    """默认预检数据库关联；显式 --sync 才写入配置的业务 API。"""
    # 只读取用户指定的环境文件，不修改任何全局配置。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=Path("runtime"))
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    # 按日期导入便于审计；幂等由 API 的不可变批次约束保证。
    indexes = [(path, json.loads(path.read_text(encoding="utf-8")))
               for path in (args.runtime_root / "web-publication").glob("*/*/*/latest.json")]
    for path, index in sorted(indexes, key=lambda item: (item[1]["business_date"], item[1]["published_at"])):
        # 必须核对数据库，即使旧回执存在也不把本地文件当成成功证据。
        result = sync_published_index(path) if args.sync else {"item_count": len(build_sync_payload(index)["rows"])}
        if result is None:
            raise ValueError("ranking_sync_not_configured")
        print(index["platform"], index["business_date"], index["batch_id"], result, flush=True)


if __name__ == "__main__":
    main()
