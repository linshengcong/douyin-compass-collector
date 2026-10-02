"""Synthetic CSV publication verifies isolated indexes and safe metric semantics."""

import csv
import gzip
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from compass_collector.exporter import TAOBAO_CSV_HEADERS
from compass_collector.oss_uploader import OssUploadError
from compass_collector.web_publisher import WebPublisher, WebPublicationSettings, WebPublicationError, _read_csv_records


class MemoryUploader:
    """Keep upload ordering observable without sending synthetic data externally."""

    # Fake settings contain no authentication information.
    settings = SimpleNamespace(valid=True)

    def __init__(self, fail_suffix=None):
        """Optionally fail one immutable object before an index can be updated."""
        # Existing latest represents the previously accepted snapshot.
        self.objects = {"compass/web/latest.json": b"old Compass index"}
        self.keys = []
        self.fail_suffix = fail_suffix

    def public_object_url(self, key):
        """Return a deterministic HTTPS resource without network access."""
        return "https://example.invalid/" + key

    def upload_public_file(self, *, file_path, object_key, **kwargs):
        """Copy a complete local artifact or raise a controlled upload failure."""
        if self.fail_suffix and object_key.endswith(self.fail_suffix):
            raise OssUploadError("oss_upload_failed")
        self.keys.append(object_key)
        self.objects[object_key] = file_path.read_bytes()


def write_taobao_csv(tmp_path, *, buyers="2.5万 ~ 5万", visitors="", url="https://sycm.taobao.com/mc/common/tm_item_redirect.htm?mi_id=synthetic"):
    """Write one clearly synthetic row using the production header contract."""
    # This file belongs to pytest temporary storage, never the account runtime.
    path = tmp_path / "taobao.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        # The ordinary writer also exercises commas and Chinese names safely.
        writer = csv.writer(handle)
        writer.writerow(TAOBAO_CSV_HEADERS)
        writer.writerow(("一级 > 二级 > 三级", 1, "", "合成商品", url, "合成店铺", buyers, visitors))
    return path


def arguments(path):
    """Provide one complete task window and publication identity."""
    return dict(csv_path=path, task_id="taobao_household_cleaning_realtime", batch_id="a" * 32,
                platform="taobao", business_date=date(2026, 10, 1), started_at=datetime(2026, 10, 1, 14),
                finished_at=datetime(2026, 10, 1, 14, 5), published_at=datetime(2026, 10, 1, 14, 6),
                successful_category_count=1, failed_category_count=0, item_count=1)


def test_taobao_snapshot_preserves_raw_and_missing_without_compass_fields(tmp_path):
    """Taobao updates its own index only after immutable data and CSV exist."""
    # The publisher under test is the production implementation.
    uploader = MemoryUploader()
    publisher = WebPublisher(WebPublicationSettings(enabled=True), uploader, runtime_root=tmp_path)
    publisher.publish(**arguments(write_taobao_csv(tmp_path)))
    # Derived keys identify both platform and task independently.
    prefix = "compass/web/taobao/taobao_household_cleaning_realtime"
    snapshot = json.loads(gzip.decompress(uploader.objects[f"{prefix}/batches/{'a' * 32}.json.gz"]))
    latest = json.loads(uploader.objects[f"{prefix}/latest.json"])
    record = snapshot["records"][0]
    assert record["pay_buyer_count"] == "2.5万 ~ 5万"
    assert record["pay_buyer_count_min_value"] == "25000.0"
    assert record["visitor_count"] is None and record["visitor_count_min_value"] is None
    assert record["newly_on_ranking"] is None
    assert "pay_amount" not in record and "pay_combo_count" not in record
    assert latest["platform"] == snapshot["platform"] == "taobao"
    assert latest["schema_version"] == snapshot["schema_version"] == 3
    assert latest["started_at"] == snapshot["started_at"] == "2026-10-01T14:00:00+08:00"
    assert latest["finished_at"] == snapshot["finished_at"] == "2026-10-01T14:05:00+08:00"
    assert uploader.keys[-1] == f"{prefix}/latest.json"
    assert uploader.objects["compass/web/latest.json"] == b"old Compass index"


@pytest.mark.parametrize("failure", [".json.gz", ".csv"])
def test_upload_failure_does_not_replace_previous_latest(tmp_path, failure):
    """Either immutable upload failing leaves both existing indexes untouched."""
    # A preexisting Taobao index must survive a partially uploaded new batch.
    uploader = MemoryUploader(failure)
    key = "compass/web/taobao/taobao_household_cleaning_realtime/latest.json"
    uploader.objects[key] = b"old Taobao index"
    publisher = WebPublisher(WebPublicationSettings(enabled=True), uploader, runtime_root=tmp_path)
    with pytest.raises(WebPublicationError):
        publisher.publish(**arguments(write_taobao_csv(tmp_path)))
    assert uploader.objects[key] == b"old Taobao index"
    assert uploader.objects["compass/web/latest.json"] == b"old Compass index"
    assert key not in uploader.keys


@pytest.mark.parametrize("changes", [{"update_legacy_index": True}, {"item_count": 2}, {"started_at": None}, {"finished_at": datetime(2026, 10, 1, 13)}])
def test_invalid_identity_window_or_count_never_uploads(tmp_path, changes):
    """Reject contract mistakes before publishing any public object."""
    # Inputs can be valid independently while still conflicting with this batch.
    uploader = MemoryUploader()
    publisher = WebPublisher(WebPublicationSettings(enabled=True), uploader, runtime_root=tmp_path)
    values = arguments(write_taobao_csv(tmp_path))
    values.update(changes)
    with pytest.raises(WebPublicationError):
        publisher.publish(**values)
    assert uploader.keys == []


@pytest.mark.parametrize("changes", [{"buyers": "garbage"}, {"url": "javascript:alert(1)"}])
def test_malformed_metric_or_link_is_not_publicly_projected(tmp_path, changes):
    """A CSV string cannot bypass the verified metric and URL parser."""
    with pytest.raises(WebPublicationError):
        _read_csv_records(write_taobao_csv(tmp_path, **changes), platform="taobao")


def test_csv_platform_contract_cannot_be_swapped(tmp_path):
    """A Taobao CSV must never be accepted as a Compass data source."""
    with pytest.raises(WebPublicationError):
        _read_csv_records(write_taobao_csv(tmp_path), platform="compass")


def test_taobao_primary_task_publishes_own_index_through_shared_runner(tmp_path, monkeypatch):
    """A Taobao-only project can publish without attempting the legacy Compass root."""
    from compass_collector import runner
    from compass_collector.config import load_config
    from compass_collector.runtime_logging import RuntimeLogger
    from pathlib import Path

    # 使用真实发布器与CSV解析；内存上传边界禁止合成商品进入真实OSS。
    uploader = MemoryUploader()
    publisher = WebPublisher(WebPublicationSettings(enabled=True, site_url="https://example.invalid"),
                             uploader, runtime_root=tmp_path)
    monkeypatch.setattr(runner.WebPublisher, "from_environment", lambda *args, **kwargs: publisher)
    monkeypatch.setattr(runner.VercelDeployer, "from_environment",
                        lambda: SimpleNamespace(settings=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(runner, "deliver_website_notification", lambda **kwargs: None)
    # 复用独立淘宝配置的合法主任务，保持实际编排入口的配置边界。
    task = load_config(Path("config/taobao-poc.yaml")).tasks[0]
    values = arguments(write_taobao_csv(tmp_path))
    candidate = runner.WebsitePublicationCandidate(
        task=task, csv_path=values["csv_path"], batch_id=values["batch_id"],
        published_at=values["published_at"], collected_batch=SimpleNamespace(
            business_date=values["business_date"], started_at=values["started_at"],
            finished_at=values["finished_at"], failed_category_count=0,
            category_runs=(SimpleNamespace(entries=(object(),)),)))
    runner._publish_website_after_collection(candidates=[candidate], oss_uploader=uploader,
        execution_batch_id="b" * 32, runtime_logger=RuntimeLogger(tmp_path / "logs"),
        primary_task_id=task.id)
    # 两个平台的旧索引隔离不能靠“整个淘宝发布被拒绝”来实现。
    assert uploader.keys[-1] == "compass/web/taobao/taobao_household_cleaning_realtime/latest.json"
    assert uploader.objects["compass/web/latest.json"] == b"old Compass index"


def test_aware_collection_window_matches_naive_sqlite_publication_time(tmp_path):
    """Live UTC times and stored Beijing wall time describe one ordered batch."""
    # runner 的采集窗口带时区，而 SQLite 读取的发布时间没有时区。
    uploader = MemoryUploader()
    publisher = WebPublisher(WebPublicationSettings(enabled=True), uploader, runtime_root=tmp_path)
    values = arguments(write_taobao_csv(tmp_path))
    values.update(started_at=datetime(2026, 10, 1, 6, tzinfo=timezone.utc),
                  finished_at=datetime(2026, 10, 1, 6, 5, tzinfo=timezone.utc))
    publisher.publish(**values)
    # 统一时区后不能报 naive/aware 比较错误，也不能把 UTC 墙上时间当成北京时间。
    latest = json.loads(uploader.objects[uploader.keys[-1]])
    assert latest["started_at"] == "2026-10-01T14:00:00+08:00"
    assert latest["finished_at"] == "2026-10-01T14:05:00+08:00"
    assert latest["published_at"] == "2026-10-01T14:06:00+08:00"


def test_full_source_third_category_name_keeps_internal_path_separator(tmp_path):
    """A Taobao third-level label can itself contain the display separator."""
    # 真实分类树有三个节点，第三个名称含>，不能当作第四级拒绝整批发布。
    path = write_taobao_csv(tmp_path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    rows[1][0] = "洗护清洁剂/卫生巾/纸/香薰 > 纸品/湿巾 > 生活用纸 > 厨房纸巾"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
    uploader = MemoryUploader()
    publisher = WebPublisher(WebPublicationSettings(enabled=True), uploader, runtime_root=tmp_path)
    publisher.publish(**arguments(path))
    # 用完整生产投影和上传顺序检查真实字段，不仅单独测试字符串拆分。
    key = f"compass/web/taobao/taobao_household_cleaning_realtime/batches/{'a' * 32}.json.gz"
    record = json.loads(gzip.decompress(uploader.objects[key]))["records"][0]
    assert (record["level1"], record["level2"], record["level3"]) == (
        "洗护清洁剂/卫生巾/纸/香薰", "纸品/湿巾", "生活用纸 > 厨房纸巾")
    assert uploader.keys[-1].endswith("/latest.json")
