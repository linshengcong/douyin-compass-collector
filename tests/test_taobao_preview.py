"""Local accepted-CSV previews must keep raw failure counts out of published rows."""
import csv
import gzip
import json
from pathlib import Path

from compass_collector.exporter import CSV_HEADERS
from scripts.taobao_web_preview import create_real_preview
from test_taobao_web_publisher import write_taobao_csv


def test_partial_manifest_preview_uses_only_complete_successful_categories(tmp_path):
    """A failed category can retain raw rows without adding them to the formal CSV."""
    # 两平台CSV均为明确合成数据，使用生产发布器生成的本地文件验证行数契约。
    compass_csv = tmp_path / "compass.csv"
    with compass_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADERS)
        writer.writerow(("合成一级 > 合成二级 > 合成三级", 1, "", "合成商品", "合成店铺",
                         "¥1-¥2", "1-2", "false"))
    taobao_csv = write_taobao_csv(tmp_path)
    # 原始响应21条，但正式CSV只接受成功分类1条，模拟真实跨页重复分类的失败收口。
    sources = []
    for platform, csv_path in (("compass", compass_csv), ("taobao", taobao_csv)):
        source = {"platform": platform, "task_id": f"{platform}_preview",
                  "batch_id": ("a" if platform == "compass" else "b") * 32,
                  "status": "partial_success", "csv_path": str(csv_path),
                  "business_date": "2026-10-01", "started_at": "2026-10-01T14:00:00+08:00",
                  "finished_at": "2026-10-01T14:01:00+08:00",
                  "published_at": "2026-10-01T14:02:00+08:00",
                  "successful_category_count": 1, "failed_category_count": 1,
                  "collected_item_count": 21,
                  "categories": [{"status": "success", "saved_item_count": 1},
                                 {"status": "failed", "saved_item_count": 20}]}
        manifest = tmp_path / f"{platform}.json"
        manifest.write_text(json.dumps(source))
        sources.append(manifest)
    # localhost模拟文件发布，合成数据不能写到真实OSS。
    root = tmp_path / "preview"
    create_real_preview(root, "http://127.0.0.1:5175", sources)
    for platform in ("compass", "taobao"):
        index = json.loads((root / f"data/{platform}/{platform}_preview/latest.json").read_text())
        data = json.loads(gzip.decompress((root / Path(index["data_url"].split(":5175/", 1)[1])).read_bytes()))
        assert index["item_count"] == len(data["records"]) == 1
        assert index["failed_category_count"] == 1
