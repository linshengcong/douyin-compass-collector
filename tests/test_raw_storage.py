"""Atomic gzip and manifest storage tests."""

import gzip
import json
from datetime import date
from pathlib import Path

from compass_collector.raw_storage import MAX_FAILURE_BODY_BYTES, RunStorage


# 原子写入测试复用唯一的真实脱敏 Fixture。
FIXTURE_PATH = Path("tests/fixtures/product_rank_page.json")


def test_gzip_page_and_manifest_are_atomically_published(tmp_path: Path) -> None:
    """Round-trip one page and finalize a successful manifest."""

    # 脱敏响应用于验证 gzip 保存后内容不变。
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 每个测试使用 pytest 独立临时 runtime 目录。
    storage = RunStorage(
        runtime_root=tmp_path,
        task_id="product_hot_sale_drinks",
        business_date=date(2026, 7, 16),
        max_items=200,
    )
    # 发布的分页路径用于回读 gzip 内容。
    page_path = storage.write_page(1, payload)
    storage.update_progress(
        api_total=200,
        target_items=200,
        saved_pages=1,
        saved_items=10,
    )
    storage.mark_success()

    with gzip.open(page_path, "rt", encoding="utf-8") as file_handle:
        # 解压后的 JSON 必须与写入前完全一致。
        restored_payload = json.load(file_handle)
    # 最终 Manifest 用于验证成功状态和进度。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))

    assert restored_payload == payload
    assert manifest["status"] == "success"
    assert manifest["saved_pages"] == 1
    assert not list(storage.run_dir.glob("*.tmp"))


def test_failure_response_is_bounded_and_atomically_published(tmp_path: Path) -> None:
    """Limit a local failure body to one MiB and record truncation safely."""

    # 独立运行目录用于验证失败材料，不污染真实 runtime。
    storage = RunStorage(
        runtime_root=tmp_path,
        task_id="product_hot_sale_drinks",
        business_date=date(2026, 7, 16),
        max_items=200,
    )
    # 比上限多一字节的正文用于触发截断。
    oversized_body = b"x" * (MAX_FAILURE_BODY_BYTES + 1)
    storage.save_failure_response(
        status_code=500,
        error_category="http_error",
        response_body=oversized_body,
    )
    # 失败索引必须仅包含脱敏摘要。
    failure_summary = json.loads(
        (storage.artifact_dir / "failure.json").read_text(encoding="utf-8")
    )
    # 失败正文文件用于校验实际落盘上限。
    response_path = storage.artifact_dir / "failure-response.txt"

    assert response_path.stat().st_size == MAX_FAILURE_BODY_BYTES
    assert failure_summary["truncated"] is True
    assert not list(storage.artifact_dir.glob("*.tmp"))
