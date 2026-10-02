"""Serve an isolated local website preview from synthetic fixtures or accepted manifests."""

import argparse
import csv
import json
import shutil
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from compass_collector.exporter import CSV_HEADERS, TAOBAO_CSV_HEADERS
from compass_collector.web_publisher import WebPublisher, WebPublicationSettings


class LocalFixtureUploader:
    """Reuse the publisher while copying explicitly selected artifacts to localhost."""

    # No credentials or remote SDK are involved in local acceptance data creation.
    settings = SimpleNamespace(valid=True)

    def __init__(self, root: Path, origin: str):
        """Bind the artifact root and the local HTTP origin."""
        # All public object keys stay below this dedicated acceptance directory.
        self.root = root
        self.origin = origin

    def public_object_url(self, key):
        """Return a localhost address instead of an externally accessible URL."""
        return f"{self.origin}/{key}"

    def upload_public_file(self, *, file_path, object_key, **kwargs):
        """Copy a complete immutable artifact into the local test hierarchy."""
        # Keys are validated by WebPublisher and contain no runtime credentials.
        destination = self.root / object_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, destination)


class PreviewHandler(SimpleHTTPRequestHandler):
    """Match OSS gzip headers so the browser exercises automatic decompression."""

    def __init__(self, *args, taobao_state="normal", **kwargs):
        """Keep synthetic failure behavior limited to this localhost preview."""
        # 仅本地验收处理器会注入错误，生产加载逻辑仍实际使用 fetch。
        self.taobao_state = taobao_state
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Return a controlled 503 for the Taobao index in the failure scenario."""
        if self.taobao_state == "error" and urlsplit(self.path).path == "/data/taobao/taobao_preview/latest.json":
            self.send_error(503, "Synthetic acceptance failure")
            return
        super().do_GET()

    def end_headers(self):
        """Declare compressed JSON exactly as the production uploader does."""
        if urlsplit(self.path).path.endswith(".json.gz"):
            self.send_header("Content-Encoding", "gzip")
        super().end_headers()


def create_fixtures(root: Path, origin: str, *, taobao_state="normal"):
    """Create two platform snapshots using the production CSV projection."""
    # An isolated local uploader prevents synthetic data from reaching real OSS.
    uploader = LocalFixtureUploader(root, origin)
    publisher = WebPublisher(WebPublicationSettings(enabled=True, public_prefix="data"), uploader, runtime_root=root / "staging")
    for platform, headers in (("compass", CSV_HEADERS), ("taobao", TAOBAO_CSV_HEADERS)):
        # 空榜仍使用正式发布器写入有效索引与快照，错误状态由本地HTTP处理器注入。
        item_count = 0 if platform == "taobao" and taobao_state == "empty" else 61
        # Sixty-one rows cover pagination, mobile reveal and a final partial page.
        csv_path = root / f"synthetic-{platform}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            # Production headers and ordinary quoting exercise the actual projection.
            writer = csv.writer(handle)
            writer.writerow(headers)
            for rank in range(1, item_count + 1):
                # Every name is marked synthetic; these are not account rankings.
                common = ("合成一级 > 合成二级 > 合成三级", rank, "", f"合成验收商品 {platform} {rank}")
                if platform == "taobao":
                    writer.writerow(common + (f"https://sycm.taobao.com/mc/common/tm_item_redirect.htm?mi_id=synthetic-{rank}", "合成店铺", "2.5万 ~ 5万", "" if rank % 3 == 0 else "0 ~ 10"))
                else:
                    writer.writerow(common + ("合成店铺", "¥1万-¥2.5万", "10-25", "true" if rank % 2 else "false"))
        publisher.publish(csv_path=csv_path, task_id=f"{platform}_preview", batch_id=("a" if platform == "compass" else "b") * 32,
                          business_date=date(2026, 10, 1), started_at=datetime(2026, 10, 1, 14), finished_at=datetime(2026, 10, 1, 14, 5),
                          published_at=datetime(2026, 10, 1, 14, 6), successful_category_count=1, failed_category_count=0, item_count=item_count, platform=platform)


def create_real_preview(root: Path, origin: str, manifests: list[Path]):
    """Project accepted CSVs through the production publisher without accessing accounts."""
    # 只允许已完成本地正式发布的真实批次，运行中、失败和dry-run不能伪装成成功数据。
    sources = [json.loads(path.read_text(encoding="utf-8")) for path in manifests]
    if {source.get("platform") for source in sources} != {"compass", "taobao"}:
        raise ValueError("Real preview requires one accepted manifest for each platform")
    if len(sources) != 2 or any(source.get("status") not in {"success", "partial_success"}
                               or not source.get("csv_path") or not source.get("published_at")
                               for source in sources):
        raise ValueError("Real preview requires completed formal publications")
    # 前端读到的batch、task、采集窗口和统计都沿用Manifest，不改写成合成身份。
    publisher = WebPublisher(WebPublicationSettings(enabled=True, public_prefix="data"),
                             LocalFixtureUploader(root, origin), runtime_root=root / "staging")
    for source in sources:
        # Manifest总条数含失败分类raw；网站只投影完整成功分类，与正式CSV保持一致。
        published_item_count = sum(category["saved_item_count"] for category in source["categories"]
                                   if category["status"] == "success")
        publisher.publish(csv_path=Path(source["csv_path"]), task_id=source["task_id"],
                          batch_id=source["batch_id"], platform=source["platform"],
                          business_date=date.fromisoformat(source["business_date"]),
                          published_at=datetime.fromisoformat(source["published_at"]),
                          started_at=datetime.fromisoformat(source["started_at"]),
                          finished_at=datetime.fromisoformat(source["finished_at"]),
                          successful_category_count=source["successful_category_count"],
                          failed_category_count=source["failed_category_count"],
                          item_count=published_item_count)


def main():
    """Prepare a disposable local preview and optionally serve it until stopped."""
    # The command never reads account profiles, raw responses, or OSS credentials.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("runtime/acceptance/taobao/2026-10-01/web-preview"))
    parser.add_argument("--port", type=int, default=5175)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--taobao-state", choices=("normal", "empty", "error"), default="normal")
    # 成对指定Manifest时使用真实已接受CSV；不允许一边真实、一边合成冒充真实验收。
    parser.add_argument("--compass-manifest", type=Path)
    parser.add_argument("--taobao-manifest", type=Path)
    arguments = parser.parse_args()
    # Only loopback is used; the development artifact is not exposed publicly.
    root = arguments.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if arguments.compass_manifest or arguments.taobao_manifest:
        if not (arguments.compass_manifest and arguments.taobao_manifest) or arguments.taobao_state != "normal":
            parser.error("Real previews require both manifests and normal state")
        create_real_preview(root, f"http://127.0.0.1:{arguments.port}",
                            [arguments.compass_manifest, arguments.taobao_manifest])
    else:
        create_fixtures(root, f"http://127.0.0.1:{arguments.port}", taobao_state=arguments.taobao_state)
    if not arguments.serve:
        return
    # The caller builds and copies frontend assets before starting this server.
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), lambda *args, **kwargs: PreviewHandler(*args, directory=str(root), taobao_state=arguments.taobao_state, **kwargs))
    print(f"Local acceptance preview: http://127.0.0.1:{arguments.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # 本地验收结束时的主动停止属于正常退出，不打印异常堆栈。
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
