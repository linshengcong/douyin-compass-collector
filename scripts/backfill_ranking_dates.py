"""从已发布的本地快照重建每日榜单入口；默认只生成文件，显式 --publish 才上传。"""

import argparse
import gzip
import json
import re
from datetime import date, datetime
from pathlib import Path

from compass_collector.oss_uploader import OssUploader
from compass_collector.web_publisher import load_web_publication_settings


def build_daily_indexes(runtime_root: Path, output_root: Path) -> list[tuple[Path, dict]]:
    """按平台、任务、日期选择发布时间最新的有效元信息，拒绝损坏快照。"""
    # 每个日期仅保留一个入口，批次文件和旧选品记录均不修改。
    daily: dict[tuple[str, str, str], dict] = {}
    for source in sorted((runtime_root / "web-publication").glob("*/*/*/latest.json")):
        # staging 文件可能来自失败发布，上传前还需核验远端批次确实存在。
        index = json.loads(source.read_text(encoding="utf-8"))
        platform, task_id, business_date = index["platform"], index["task_id"], index["business_date"]
        if platform not in {"compass", "taobao"} or not re.fullmatch(r"[a-z][a-z0-9_]*", task_id):
            raise ValueError("invalid snapshot identity")
        date.fromisoformat(business_date)
        # 元信息和快照必须同属一个不可变批次，不能仅凭目录名生成索引。
        with gzip.open(source.parent / "data.json.gz", "rt", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        if any(snapshot.get(field) != index.get(field) for field in
               ("platform", "task_id", "batch_id", "business_date", "published_at", "schema_version")):
            raise ValueError("snapshot metadata mismatch")
        if len(snapshot["records"]) != index["item_count"]:
            raise ValueError("snapshot count mismatch")
        # ISO 日期先解析为带时区时间，避免仅字符串比较不同偏移值。
        key = (platform, task_id, business_date)
        published_at = datetime.fromisoformat(index["published_at"])
        if published_at.tzinfo is None:
            raise ValueError("snapshot timezone missing")
        if key not in daily or published_at > datetime.fromisoformat(daily[key]["published_at"]):
            daily[key] = index
    # 输出目录可供审阅或作为本地静态服务器的日期索引根目录。
    generated: list[tuple[Path, dict]] = []
    for (platform, task_id, business_date), index in sorted(daily.items()):
        target = output_root / platform / task_id / "dates" / f"{business_date}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        generated.append((target, index))
    return generated


def main() -> None:
    """默认离线生成；发布时先核验所有远端文件，再只写 dates/ 入口。"""
    # 输出路径由操作者提供，避免默认向仓库写入真实业务快照。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    arguments = parser.parse_args()
    generated = build_daily_indexes(arguments.runtime_root, arguments.output_dir)
    if arguments.publish:
        # 凭证只由现有环境读取；工具不载入或修改任何全局配置文件。
        import httpx
        uploader = OssUploader.from_environment()
        settings = load_web_publication_settings()
        if not settings.valid or not uploader.settings.valid:
            raise ValueError("website publication must be enabled")
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for _, index in generated:
                # URL 使用当前配置重新生成并对比，不能请求元信息中的任意外部地址。
                prefix = f"{settings.public_prefix}/{index['platform']}/{index['task_id']}"
                expected = uploader.public_object_url(f"{prefix}/batches/{index['batch_id']}.json.gz")
                if expected != index["data_url"]:
                    raise ValueError("snapshot URL does not match configured source")
                # 必须匹配完整快照，避免本地 staging 文件对应未成功发布的批次。
                response = client.get(expected)
                response.raise_for_status()
                raw = response.content
                remote = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
                if any(remote.get(field) != index.get(field) for field in ("batch_id", "business_date", "published_at")):
                    raise ValueError("remote snapshot metadata mismatch")
                csv_url = uploader.public_object_url(f"{prefix}/batches/{index['batch_id']}.csv")
                if csv_url != index["csv_url"]:
                    raise ValueError("CSV URL does not match configured source")
                client.head(csv_url).raise_for_status()
                # 不覆盖已有日期入口，重复运行也不会把新版本降级成较旧本地快照。
                existing = client.get(uploader.public_object_url(f"{prefix}/dates/{index['business_date']}.json"))
                if existing.status_code != 404:
                    existing.raise_for_status()
                    if existing.json() != index:
                        raise ValueError("daily index already exists with different metadata; inspect before publishing")
        for path, index in generated:
            uploader.upload_public_file(file_path=path,
                object_key=f"{settings.public_prefix}/{index['platform']}/{index['task_id']}/dates/{index['business_date']}.json",
                content_type="application/json", cache_control="no-store, max-age=0")
    for _, index in generated:
        print(f"{index['platform']} {index['business_date']} {index['batch_id']}")
    print(f"{'Published' if arguments.publish else 'Generated locally'} {len(generated)} daily indexes")


if __name__ == "__main__":
    main()
