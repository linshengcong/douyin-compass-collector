"""Stage-three category raw files, failure artifacts, and Manifest sync tests."""

import gzip
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

import compass_collector.raw_storage as raw_storage_module
from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    DiscoveredCategory,
    RawPageRecord,
)
from compass_collector.persistence import Database, upgrade_database
from compass_collector.raw_storage import BatchStorage


# 固定批次时间用于 Manifest 和 SQLite 的可重复断言。
PLANNED_AT = datetime(2026, 7, 17, 14, 0)
# raw 与状态迁移使用独立时间，便于识别字段来源。
CAPTURED_AT = datetime(2026, 7, 17, 14, 0, 2)


def prepare_storage_and_database(
    tmp_path: Path,
) -> tuple[BatchStorage, Database, tuple[CategoryRunPlan, ...]]:
    """Prepare one stage-two batch ready for stage-three category pages."""

    # BatchStorage 创建批次唯一 Manifest 和 categories 目录。
    storage = BatchStorage(
        runtime_root=tmp_path / "runtime",
        batch_id="batch-stage-three-storage",
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=PLANNED_AT,
        mode="normal",
        started_at=PLANNED_AT,
    )
    # SQLite 使用临时迁移数据库作为 Manifest 的权威状态源。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    database.create_batch(
        batch_id=storage.batch_id,
        task_id=storage.task_id,
        business_date=date(2026, 7, 17),
        planned_at=PLANNED_AT,
        mode="normal",
        brand_type=0,
        price_bin="10001-?",
        manifest_path=storage.manifest_path,
        started_at=PLANNED_AT,
    )
    # 分类树先原子落盘，再分别登记 SQLite 与 Manifest。
    category_tree_path = storage.write_category_tree({"data": {"cate_list": []}})
    database.record_category_tree_raw(
        batch_id=storage.batch_id,
        category_tree_raw_path=category_tree_path,
    )
    storage.record_category_tree_saved(
        category_tree_path,
        captured_at=CAPTURED_AT,
    )
    # 两个分类用于覆盖当前分类与剩余 pending 分类。
    categories = (
        DiscoveredCategory(
            discovery_order=1,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="200",
            level2_category_name="休闲食品",
            category_id="301",
            category_name="海味零食",
        ),
        DiscoveredCategory(
            discovery_order=2,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="201",
            level2_category_name="水饮冲调",
            category_id="302",
            category_name="茶叶",
        ),
    )
    # 发现结果与分类运行 ID 由数据库和 Manifest 共同消费。
    discovery = CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=categories,
    )
    category_run_plans = tuple(
        CategoryRunPlan(
            category_run_id=f"category-run-{category.discovery_order}",
            category=category,
        )
        for category in categories
    )
    database.create_category_runs(
        batch_id=storage.batch_id,
        discovery=discovery,
        category_run_plans=category_run_plans,
    )
    storage.record_discovered_categories(discovery, category_run_plans)
    return storage, database, category_run_plans


def test_category_pages_and_failure_artifacts_are_isolated_by_category(
    tmp_path: Path,
) -> None:
    """Write category pages and bounded diagnostics below stable run IDs."""

    # 已登记分类是 BatchStorage 允许写入 raw 的白名单。
    storage, database, category_run_plans = prepare_storage_and_database(tmp_path)
    try:
        category_run_id = category_run_plans[0].category_run_id
        # 页面 payload 模拟已经通过页级契约校验的完整响应。
        page_payload: dict[str, Any] = {"data": {"total": 1, "data_result": [{}]}}
        page_path = storage.write_category_page(
            category_run_id,
            1,
            page_payload,
        )
        # 同一分类同一页不能覆盖已有 raw。
        with pytest.raises(FileExistsError):
            storage.write_category_page(category_run_id, 1, page_payload)
        # 未登记或路径逃逸 ID 都不能创建目录。
        with pytest.raises((ValueError, RuntimeError)):
            storage.write_category_page("../outside", 1, page_payload)
        # 分类失败正文和安全索引保存在独立 artifact 子目录。
        storage.save_category_failure(
            category_run_id=category_run_id,
            failed_page=2,
            status_code=500,
            error_category="request_failed",
            response_body=b"temporary upstream failure",
            failed_step="request_product_rank_page",
            exception_type="HttpRequestError",
            safe_endpoint_path="/compass_api/shop/product/product_rank/market_hot_sale",
        )
    finally:
        database.close()

    assert page_path == (
        storage.categories_dir / category_run_id / "page-001.json.gz"
    )
    with gzip.open(page_path, "rt", encoding="utf-8") as file_handle:
        # gzip 恢复内容必须与传入 payload 完全一致。
        restored_payload = json.load(file_handle)
    # failure.json 不复制失败响应正文。
    failure_path = storage.artifact_dir / category_run_id / "failure.json"
    failure_summary = json.loads(failure_path.read_text(encoding="utf-8"))

    assert restored_payload == page_payload
    assert failure_summary["category_run_id"] == category_run_id
    assert failure_summary["failed_page"] == 2
    assert "temporary upstream failure" not in json.dumps(failure_summary)
    assert (
        storage.artifact_dir / category_run_id / "failure-response.txt"
    ).read_bytes() == b"temporary upstream failure"


def test_manifest_sync_uses_one_authoritative_snapshot_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the old Manifest intact on write failure and retry the same snapshot."""

    # SQLite 状态与 BatchStorage 初始 Manifest 都已完成分类登记。
    storage, database, category_run_plans = prepare_storage_and_database(tmp_path)
    try:
        category_run_id = category_run_plans[0].category_run_id
        # running 快照先成功同步，作为后续失败测试的旧状态。
        running_snapshot = database.start_category_run(
            category_run_id,
            CAPTURED_AT,
        )
        storage.sync_collection_snapshot(running_snapshot)
        # 页级 raw 先落盘，再构造数据库 RawPageRecord。
        page_payload: dict[str, Any] = {"data": {"total": 10, "data_result": []}}
        page_path = storage.write_category_page(
            category_run_id,
            1,
            page_payload,
        )
        raw_page = RawPageRecord(
            page_no=1,
            path=page_path,
            item_count=10,
            captured_at=CAPTURED_AT,
        )
        # 新快照包含一页十条，但暂时还没有写入 Manifest。
        page_snapshot = database.record_category_page(
            category_run_id,
            raw_page,
            10,
            1,
        )
        # 写入失败前保存文件和内存旧状态用于精确对比。
        old_manifest_file = storage.manifest_path.read_text(encoding="utf-8")
        old_manifest_memory = json.loads(json.dumps(storage.manifest))
        # 原始原子写函数供第二次调用恢复正常行为。
        original_writer = raw_storage_module._write_json_atomic
        # 调用计数让第一次同步失败、第二次使用同一快照成功。
        write_attempts = {"count": 0}

        def flaky_manifest_writer(path: Path, payload: dict[str, Any]) -> None:
            """Fail only the first target Manifest replacement."""

            # 只拦截目标 Manifest，其他潜在 JSON 写入保持原行为。
            if path == storage.manifest_path and write_attempts["count"] == 0:
                write_attempts["count"] += 1
                raise OSError("simulated manifest replacement failure")
            original_writer(path, payload)

        monkeypatch.setattr(
            raw_storage_module,
            "_write_json_atomic",
            flaky_manifest_writer,
        )
        with pytest.raises(OSError, match="replacement failure"):
            storage.sync_collection_snapshot(page_snapshot)
        # 失败后内存和正式文件均保持旧快照，允许安全重试。
        assert storage.manifest == old_manifest_memory
        assert storage.manifest_path.read_text(encoding="utf-8") == old_manifest_file
        storage.sync_collection_snapshot(page_snapshot)
    finally:
        database.close()

    # 第二次同步完整替换批次和分类进度，没有中间状态。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["saved_page_count"] == 1
    assert manifest["collected_item_count"] == 10
    assert manifest["categories"][0]["saved_page_count"] == 1
    assert manifest["categories"][0]["saved_item_count"] == 10
    assert manifest["categories"][1]["status"] == "pending"
