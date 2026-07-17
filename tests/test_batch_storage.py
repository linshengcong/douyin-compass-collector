"""Batch-level raw directory and single-Manifest storage tests."""

import gzip
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    DiscoveredCategory,
)
from compass_collector.persistence import BatchCollectionSnapshot, CategoryRunSnapshot
from compass_collector.raw_storage import MAX_FAILURE_BODY_BYTES, BatchStorage


# 测试时间统一使用工程约定的北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 固定计划时间和开始时间使 Manifest 断言可重复。
PLANNED_AT = datetime(2026, 7, 17, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
STARTED_AT = datetime(2026, 7, 17, 13, 59, tzinfo=SHANGHAI_TIMEZONE)
FINISHED_AT = datetime(2026, 7, 17, 14, 1, tzinfo=SHANGHAI_TIMEZONE)


def build_storage(tmp_path: Path) -> BatchStorage:
    """Create one deterministic stage-two batch below pytest's temp directory."""

    return BatchStorage(
        runtime_root=tmp_path,
        batch_id="a" * 32,
        task_id="product_hot_sale_food_level3",
        business_date=date(2026, 7, 17),
        planned_at=PLANNED_AT,
        mode="normal",
        started_at=STARTED_AT,
    )


def build_discovery() -> CategoryDiscoveryResult:
    """Build two ordered level-three categories with one shared root."""

    # 两个分类覆盖不同二级路径并保持接口发现顺序。
    categories = (
        DiscoveredCategory(
            discovery_order=1,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="100",
            level2_category_name="休闲食品",
            category_id="1001",
            category_name="海味零食",
        ),
        DiscoveredCategory(
            discovery_order=2,
            level1_category_id="13",
            level1_category_name="食品饮料",
            level2_category_id="200",
            level2_category_name="水饮冲调",
            category_id="2001",
            category_name="茶叶",
        ),
    )
    return CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=categories,
    )


def prepare_registered_storage(
    tmp_path: Path,
) -> tuple[BatchStorage, CategoryDiscoveryResult, tuple[CategoryRunPlan, ...]]:
    """Prepare one batch whose category tree and category plans are registered."""

    # 批次先创建唯一 Manifest 和分类 raw 目录边界。
    storage = build_storage(tmp_path)
    # 分类树必须在分类计划之前原子落盘并登记。
    category_tree_path = storage.write_category_tree(
        {"data": {"cate_list": []}, "msg": "success", "st": 0}
    )
    storage.record_category_tree_saved(category_tree_path, captured_at=STARTED_AT)
    # 固定发现结果用于后续分类失败与最终快照测试。
    discovery = build_discovery()
    # 稳定分类运行 ID 让 raw、Manifest 与诊断目录可精确断言。
    category_run_plans = (
        CategoryRunPlan(category_run_id="1" * 32, category=discovery.categories[0]),
        CategoryRunPlan(category_run_id="2" * 32, category=discovery.categories[1]),
    )
    storage.record_discovered_categories(discovery, category_run_plans)
    return storage, discovery, category_run_plans


def test_batch_storage_creates_the_accepted_directory_and_initial_manifest(
    tmp_path: Path,
) -> None:
    """Create one batch directory without putting category names into paths."""

    # BatchStorage 初始化应立即发布唯一的运行中 Manifest。
    storage = build_storage(tmp_path)
    # 回读 Manifest 用于核对其与 collection_batches 的初始字段。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))
    # batch_id 目录是 raw 数据的唯一批次边界。
    expected_batch_dir = (
        tmp_path
        / "raw"
        / "2026-07-17"
        / "product_hot_sale_food_level3"
        / ("a" * 32)
    )

    assert storage.batch_dir == expected_batch_dir
    assert storage.categories_dir == expected_batch_dir / "categories"
    assert manifest["batch_id"] == "a" * 32
    assert manifest["status"] == "running"
    assert manifest["mode"] == "normal"
    assert manifest["version"] is None
    assert manifest["discovered_category_count"] == 0
    assert manifest["categories"] == []
    assert list(storage.batch_dir.rglob("manifest.json")) == [storage.manifest_path]
    assert "食品饮料" not in str(storage.batch_dir)
    assert not list(storage.batch_dir.rglob("*.tmp"))


def test_category_tree_is_atomically_gzipped_and_never_overwritten(
    tmp_path: Path,
) -> None:
    """Save the complete category response once and index it in the Manifest."""

    # 独立批次用于验证分类树 gzip 往返和覆盖保护。
    storage = build_storage(tmp_path)
    # 小型完整响应保留顶层 data、msg 和 st 契约。
    payload = {
        "data": {"cate_list": [{"cate_id": "13", "cate_name": "食品饮料"}]},
        "msg": "success",
        "st": 0,
    }
    # 首次写入返回固定 category-tree.json.gz 路径。
    category_tree_path = storage.write_category_tree(payload)
    storage.record_category_tree_saved(category_tree_path, captured_at=STARTED_AT)
    with gzip.open(category_tree_path, "rt", encoding="utf-8") as file_handle:
        # 解压内容必须与原始完整 JSON 一致。
        restored_payload = json.load(file_handle)
    # 最新 Manifest 必须只记录安全本地路径，不复制响应正文。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))

    assert category_tree_path == storage.batch_dir / "category-tree.json.gz"
    assert restored_payload == payload
    assert manifest["category_tree_raw_path"] == str(category_tree_path)
    assert manifest["category_tree_captured_at"] == STARTED_AT.isoformat()
    assert "cate_list" not in manifest
    with pytest.raises(FileExistsError):
        storage.write_category_tree({"data": {"cate_list": []}})
    assert not list(storage.batch_dir.rglob("*.tmp"))


def test_discovered_categories_are_recorded_in_one_batch_manifest(
    tmp_path: Path,
) -> None:
    """Mirror ordered category-run plans without creating category manifests."""

    # 分类树必须先于解析后的分类索引落盘。
    storage = build_storage(tmp_path)
    category_tree_path = storage.write_category_tree(
        {"data": {"cate_list": []}, "msg": "success", "st": 0}
    )
    storage.record_category_tree_saved(category_tree_path, captured_at=STARTED_AT)
    # discovery 是纯分类快照，category_run_id 由 runner 层提前分配。
    discovery = build_discovery()
    category_run_plans = (
        CategoryRunPlan(category_run_id="1" * 32, category=discovery.categories[0]),
        CategoryRunPlan(category_run_id="2" * 32, category=discovery.categories[1]),
    )
    storage.record_discovered_categories(discovery, category_run_plans)
    # 回读唯一 Manifest 验证数据库同名字段与原始发现顺序。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))

    assert manifest["root_category_id"] == "13"
    assert manifest["root_category_name"] == "食品饮料"
    assert manifest["discovered_category_count"] == 2
    assert [item["category_run_id"] for item in manifest["categories"]] == [
        "1" * 32,
        "2" * 32,
    ]
    assert [item["discovery_order"] for item in manifest["categories"]] == [1, 2]
    assert [item["status"] for item in manifest["categories"]] == [
        "pending",
        "pending",
    ]
    assert list(storage.categories_dir.iterdir()) == []
    assert list(storage.batch_dir.rglob("manifest.json")) == [storage.manifest_path]
    assert "海味零食" not in str(storage.batch_dir)
    assert "茶叶" not in str(storage.batch_dir)


def test_zero_category_discovery_finishes_as_failed_without_category_entries(
    tmp_path: Path,
) -> None:
    """Keep the raw tree while marking an empty discovery as a failed batch."""

    # 空分类失败仍应保留本次成功获取的完整分类树响应。
    storage = build_storage(tmp_path)
    category_tree_path = storage.write_category_tree(
        {"data": {"cate_list": []}, "msg": "success", "st": 0}
    )
    storage.record_category_tree_saved(category_tree_path, captured_at=STARTED_AT)
    # 终态接口允许保存已定位到的根分类，但不伪造 category_run。
    storage.mark_batch_terminal(
        status="failed",
        error_category="category_discovery_empty",
        finished_at=FINISHED_AT,
        root_category_id="13",
        root_category_name="食品饮料",
    )
    # 最终 Manifest 用于核对零分类状态与 SQLite 约束一致。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))

    assert manifest["status"] == "failed"
    assert manifest["error_category"] == "category_discovery_empty"
    assert manifest["root_category_id"] == "13"
    assert manifest["discovered_category_count"] == 0
    assert manifest["successful_category_count"] == 0
    assert manifest["failed_category_count"] == 0
    assert manifest["not_started_category_count"] == 0
    assert manifest["categories"] == []
    assert manifest["finished_at"] == FINISHED_AT.isoformat()
    assert storage.category_tree_path.exists()
    assert not list(storage.batch_dir.rglob("*.tmp"))


def test_terminal_batch_marks_all_pending_categories_not_started(
    tmp_path: Path,
) -> None:
    """Keep Manifest category states aligned with a discovery-stage failure."""

    # 先登记完整分类计划，模拟数据库成功后 Manifest 或编排层异常。
    storage = build_storage(tmp_path)
    category_tree_path = storage.write_category_tree(
        {"data": {"cate_list": []}, "msg": "success", "st": 0}
    )
    storage.record_category_tree_saved(category_tree_path, captured_at=STARTED_AT)
    # 两个固定分类运行 ID 用于核对所有 pending 状态都被收口。
    discovery = build_discovery()
    category_run_plans = (
        CategoryRunPlan(category_run_id="1" * 32, category=discovery.categories[0]),
        CategoryRunPlan(category_run_id="2" * 32, category=discovery.categories[1]),
    )
    storage.record_discovered_categories(discovery, category_run_plans)
    storage.mark_batch_terminal(
        status="failed",
        error_category="internal_error",
        finished_at=FINISHED_AT,
    )
    # 回读正式 Manifest，避免只断言内存字典。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))

    assert manifest["not_started_category_count"] == 2
    assert [category["status"] for category in manifest["categories"]] == [
        "not_started",
        "not_started",
    ]
    assert [category["finished_at"] for category in manifest["categories"]] == [
        FINISHED_AT.isoformat(),
        FINISHED_AT.isoformat(),
    ]


def test_batch_failure_response_is_bounded_and_sanitized(tmp_path: Path) -> None:
    """Keep a local response body without leaking it into failure.json."""

    # 超过上限一字节的正文用于验证批次级失败材料截断。
    storage = build_storage(tmp_path)
    oversized_body = b"x" * (MAX_FAILURE_BODY_BYTES + 1)
    storage.save_failure(
        status_code=500,
        error_category="http_error",
        response_body=oversized_body,
        failed_step="category_tree_request_or_contract",
        exception_type="HttpResponseError",
        safe_endpoint_path="/compass_api/config_center/category/cate_list",
    )
    # failure.json 只包含安全索引，不包含响应正文。
    failure_summary = json.loads(
        (storage.artifact_dir / "failure.json").read_text(encoding="utf-8")
    )
    # failure-response.txt 是唯一保存正文的位置。
    response_path = storage.artifact_dir / "failure-response.txt"

    assert response_path.stat().st_size == MAX_FAILURE_BODY_BYTES
    assert failure_summary["batch_id"] == "a" * 32
    assert failure_summary["truncated"] is True
    assert failure_summary["saved_bytes"] == MAX_FAILURE_BODY_BYTES
    assert "xxxxx" not in json.dumps(failure_summary)
    assert not list(storage.artifact_dir.rglob("*.tmp"))


def test_batch_failure_without_response_body_only_writes_safe_index(
    tmp_path: Path,
) -> None:
    """Do not create a response artifact when the request produced no body."""

    # 独立批次模拟连接失败等没有 HTTP 响应正文的场景。
    storage = build_storage(tmp_path)
    storage.save_failure(
        status_code=None,
        error_category="network_error",
        response_body=None,
        failed_step="category_tree_request_or_contract",
        exception_type="HttpRequestError",
        safe_endpoint_path="/compass_api/config_center/category/cate_list",
    )
    # 安全索引必须明确响应未保存且大小为零。
    failure_summary = json.loads(
        (storage.artifact_dir / "failure.json").read_text(encoding="utf-8")
    )
    # 无正文时不能创建空的 failure-response.txt 误导人工诊断。
    response_path = storage.artifact_dir / "failure-response.txt"

    assert failure_summary["response_saved"] is False
    assert failure_summary["saved_bytes"] == 0
    assert failure_summary["truncated"] is False
    assert not response_path.exists()
    assert not list(storage.artifact_dir.rglob("*.tmp"))


def test_category_failure_response_is_truncated_to_one_mib(tmp_path: Path) -> None:
    """Bound each category failure body independently below its run directory."""

    # 已登记分类是允许创建分类级故障材料的白名单。
    storage, _, category_run_plans = prepare_registered_storage(tmp_path)
    # 首个分类运行 ID 用于定位隔离后的 artifact 子目录。
    category_run_id = category_run_plans[0].category_run_id
    # 超出上限的分类响应用于验证逐分类截断行为。
    oversized_body = b"y" * (MAX_FAILURE_BODY_BYTES + 17)
    storage.save_category_failure(
        category_run_id=category_run_id,
        failed_page=2,
        status_code=500,
        error_category="request_failed",
        response_body=oversized_body,
        failed_step="request_product_rank_page",
        exception_type="HttpResponseError",
        safe_endpoint_path="/compass_api/shop/product/product_rank/market_hot_sale",
    )
    # 分类级目录同时包含有限正文和不含正文内容的安全索引。
    category_artifact_dir = storage.artifact_dir / category_run_id
    # 正文文件必须恰好停在全局 1 MiB 上限。
    response_path = category_artifact_dir / "failure-response.txt"
    # failure.json 用于核对截断元数据和分类身份。
    failure_summary = json.loads(
        (category_artifact_dir / "failure.json").read_text(encoding="utf-8")
    )

    assert response_path.stat().st_size == MAX_FAILURE_BODY_BYTES
    assert failure_summary["category_run_id"] == category_run_id
    assert failure_summary["response_saved"] is True
    assert failure_summary["saved_bytes"] == MAX_FAILURE_BODY_BYTES
    assert failure_summary["truncated"] is True
    assert "yyyyy" not in json.dumps(failure_summary)
    assert not list(category_artifact_dir.rglob("*.tmp"))


def test_browser_failure_only_saves_safe_page_metadata_and_png(tmp_path: Path) -> None:
    """Persist a screenshot without retaining a full URL or an unbounded title."""

    # 浏览器失败材料使用独立批次级 artifact 目录。
    storage = build_storage(tmp_path)
    # PNG 字节只需验证存储层原样发布，不在此测试图像解码。
    screenshot = b"\x89PNG\r\n\x1a\nfixture"
    # 超长标题验证存储层会在错误边界之外再次限制长度。
    page_title = "页" * 205
    storage.save_browser_failure(
        error_category="browser_error",
        failed_step="wait_for_login",
        exception_type="BrowserLoginError",
        safe_page_path="/shop/chance/rank-product",
        page_title=page_title,
        screenshot=screenshot,
    )
    # 安全索引只允许固定路径、有限标题和截图存在标记。
    failure_summary = json.loads(
        (storage.artifact_dir / "failure.json").read_text(encoding="utf-8")
    )
    # 序列化文本用于确认没有完整 URL 主机或查询参数泄漏。
    serialized_summary = json.dumps(failure_summary, ensure_ascii=False)

    assert (storage.artifact_dir / "failure.png").read_bytes() == screenshot
    assert failure_summary["safe_page_path"] == "/shop/chance/rank-product"
    assert failure_summary["page_title"] == page_title[:200]
    assert failure_summary["screenshot_saved"] is True
    assert "https://compass.jinritemai.com" not in serialized_summary
    assert "?" not in failure_summary["safe_page_path"]
    assert not list(storage.artifact_dir.rglob("*.tmp"))


@pytest.mark.parametrize(
    "unsafe_page_path",
    [
        # 查询串可能包含跟踪参数或令牌，不能进入诊断索引。
        "/shop/chance/rank-product?from_page=/shop",
        # fragment 同样不属于允许保存的固定安全路径。
        "/shop/chance/rank-product#ranking",
    ],
)
def test_browser_failure_rejects_query_and_fragment_paths(
    tmp_path: Path,
    unsafe_page_path: str,
) -> None:
    """Reject browser paths that could retain query or fragment data."""

    # 每个参数化用例使用独立批次，避免 artifact 状态相互影响。
    storage = build_storage(tmp_path)

    with pytest.raises(ValueError, match="safe page path"):
        storage.save_browser_failure(
            error_category="browser_error",
            failed_step="wait_for_login",
            exception_type="BrowserLoginError",
            safe_page_path=unsafe_page_path,
            page_title=None,
            screenshot=None,
        )

    assert not storage.artifact_dir.exists()


@pytest.mark.parametrize(
    (
        "batch_status",
        "second_category_status",
        "successful_category_count",
        "failed_category_count",
        "batch_error_category",
    ),
    [
        # 所有三级分类成功时正式发布 success 版本。
        ("success", "success", 2, 0, None),
        # 少量分类失败时仍正式发布 partial_success 版本。
        (
            "partial_success",
            "failed",
            1,
            1,
            "category_collection_partial_failure",
        ),
    ],
)
def test_final_collection_snapshot_publishes_one_authoritative_manifest(
    tmp_path: Path,
    batch_status: str,
    second_category_status: str,
    successful_category_count: int,
    failed_category_count: int,
    batch_error_category: str | None,
) -> None:
    """Project both success and partial-success publication into one Manifest."""

    # 正式快照只能覆盖已登记且身份不变的分类集合。
    storage, discovery, category_run_plans = prepare_registered_storage(tmp_path)
    # 第一分类完整成功，提供两页十五条的稳定基线。
    first_category_snapshot = CategoryRunSnapshot(
        category_run_id=category_run_plans[0].category_run_id,
        batch_id=storage.batch_id,
        discovery_order=1,
        level1_category_id=discovery.categories[0].level1_category_id,
        level1_category_name=discovery.categories[0].level1_category_name,
        level2_category_id=discovery.categories[0].level2_category_id,
        level2_category_name=discovery.categories[0].level2_category_name,
        category_id=discovery.categories[0].category_id,
        category_name=discovery.categories[0].category_name,
        status="success",
        api_total=15,
        target_page_count=2,
        saved_page_count=2,
        saved_item_count=15,
        failed_page=None,
        error_category=None,
        started_at=PLANNED_AT,
        finished_at=FINISHED_AT,
    )
    # 第二分类根据参数覆盖完整成功与第二页失败两种正式发布结果。
    second_category_snapshot = CategoryRunSnapshot(
        category_run_id=category_run_plans[1].category_run_id,
        batch_id=storage.batch_id,
        discovery_order=2,
        level1_category_id=discovery.categories[1].level1_category_id,
        level1_category_name=discovery.categories[1].level1_category_name,
        level2_category_id=discovery.categories[1].level2_category_id,
        level2_category_name=discovery.categories[1].level2_category_name,
        category_id=discovery.categories[1].category_id,
        category_name=discovery.categories[1].category_name,
        status=second_category_status,
        api_total=20 if second_category_status == "failed" else 10,
        target_page_count=2 if second_category_status == "failed" else 1,
        saved_page_count=1,
        saved_item_count=10,
        failed_page=2 if second_category_status == "failed" else None,
        error_category=(
            "request_failed" if second_category_status == "failed" else None
        ),
        started_at=PLANNED_AT,
        finished_at=FINISHED_AT,
    )
    # 中文 CSV 文件名验证 Manifest 只投影最终安全本地路径。
    csv_path = tmp_path / "exports" / "2026-07-17" / "食品饮料三级分类榜单-v1.csv"
    # SQLite 正式快照是 Manifest 状态、统计和发布时间的唯一来源。
    final_snapshot = BatchCollectionSnapshot(
        batch_id=storage.batch_id,
        task_id=storage.task_id,
        business_date=storage.business_date,
        planned_at=PLANNED_AT,
        mode="normal",
        status=batch_status,
        version=1,
        brand_type=0,
        price_bin="10001-?",
        root_category_id=discovery.root_category_id,
        root_category_name=discovery.root_category_name,
        manifest_path=str(storage.manifest_path),
        category_tree_raw_path=str(storage.category_tree_path),
        csv_path=str(csv_path),
        discovered_category_count=2,
        successful_category_count=successful_category_count,
        failed_category_count=failed_category_count,
        not_started_category_count=0,
        saved_page_count=3,
        collected_item_count=25,
        error_category=batch_error_category,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        published_at=FINISHED_AT,
        categories=(first_category_snapshot, second_category_snapshot),
    )
    storage.sync_collection_snapshot(final_snapshot)
    # 回读正式文件，避免只断言存储实例的内存镜像。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))

    assert manifest["status"] == batch_status
    assert manifest["version"] == 1
    assert manifest["brand_type"] == 0
    assert manifest["price_bin"] == "10001-?"
    assert manifest["csv_path"] == str(csv_path)
    assert manifest["published_at"] == FINISHED_AT.isoformat()
    assert manifest["successful_category_count"] == successful_category_count
    assert manifest["failed_category_count"] == failed_category_count
    assert manifest["saved_page_count"] == 3
    assert manifest["collected_item_count"] == 25
    assert [category["status"] for category in manifest["categories"]] == [
        "success",
        second_category_status,
    ]
    assert manifest["categories"][1]["saved_item_count"] == 10
    assert manifest["categories"][1]["failed_page"] == (
        2 if second_category_status == "failed" else None
    )
    assert not list(storage.batch_dir.rglob("*.tmp"))
