"""Synthetic UI events through actual raw, SQLite, Manifest and CSV lifecycle."""

import csv
import gzip
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from compass_collector import category_batch, category_collection
from compass_collector.category_batch import prepare_category_batch
from compass_collector.category_collection import collect_category_batch
from compass_collector.exporter import CsvExporter, TAOBAO_CSV_HEADERS
from compass_collector.errors import BrowserOperationError, CategoryBatchCollectionError, ResponseContractError
from compass_collector.persistence import CategoryRun, CollectionBatch, Database, ProductRankEntryModel, RawResponse, upgrade_database
from compass_collector.runtime_logging import RuntimeLogger
from test_taobao_adapter import setup
from test_taobao_capture import Request, Response
from test_taobao_product_rank import CAPTURED_AT


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("total,lost_page", [(0, None), (1, None), (41, None), (121, 6)])
@pytest.mark.parametrize("repeated_product", [False, True])
def test_adapter_to_official_publication_keeps_platform_and_page_evidence(tmp_path, monkeypatch, partial, total, lost_page, repeated_product):
    """No prebuilt batch or post-hoc platform mutation may substitute for this path."""
    # 所有账号数据均为合成；真实页面控件与 registry 不在本地链路验证范围。
    adapter, controls, task, scope, closed = setup(monkeypatch)
    controls.total = total
    # 第六页活动页码故障通过真实编排验证raw/SQLite/CSV均不重复落盘。
    confirmation_failures = []
    if lost_page is not None:
        original_confirm = controls.confirm_page

        def confirm_with_one_lost_active_page(page, page_no, api_total):
            """Reset the visible position once after a matching response completes."""
            original_confirm(page, page_no, api_total)
            if page_no == lost_page and not confirmation_failures:
                confirmation_failures.append(True)
                controls.page_no = 1
                raise BrowserOperationError("Active page lost", category="browser_page_error",
                                            failed_step="taobao_active_page", exception_type="RuntimeError")

        monkeypatch.setattr(controls, "confirm_page", confirm_with_one_lost_active_page)
    if repeated_product:
        # 重复身份经真实响应监听、分页解析和数据库发布，不能仅检查内存对象。
        original_emit = controls.emit

        def emit_repeated_product(response):
            """Keep source ranks distinct while repeating one item across pages."""
            if response.payload.get("code") == 0 and isinstance(response.payload.get("data"), dict) and "recordCount" in response.payload["data"].get("data", {}):
                for row in response.payload["data"]["data"]["data"]:
                    row["itemId"]["value"] = "100001"
                    row["item"]["itemId"] = "100001"
            original_emit(response)

        monkeypatch.setattr(controls, "emit", emit_repeated_product)
    monkeypatch.setattr(category_batch, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    monkeypatch.setattr(category_collection, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    if partial:
        # 仅第二个三级分类失败，成功分类应完整发布，失败数据不能混入。
        original_rank = controls.rank

        def rank_or_fail(page):
            """Emit a genuine nonzero business envelope through production listeners."""
            if controls.scope.key == "216502":
                from compass_collector.platforms.taobao_product_rank import build_expected_params
                # 失败同样必须匹配本分类业务参数，不注入共享编排的结果对象。
                params = build_expected_params(task, controls.scope, CAPTURED_AT.date(), controls.page_no)
                controls.emit(Response(Request(page, params), {"code": 12345, "message": "synthetic failure"}))
            else:
                original_rank(page)

        monkeypatch.setattr(controls, "rank", rank_or_fail)
    # 真实 Alembic 迁移、真实本地存储与运行日志隔离在 pytest 目录。
    runtime = tmp_path / "runtime"
    database_path = runtime / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    logger = RuntimeLogger(runtime / "logs")
    try:
        prepared = prepare_category_batch(runtime_root=runtime, batch_id="a" * 32, task=task,
                                          business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT,
                                          mode="normal", client=adapter, database=database, runtime_logger=logger)
        assert [plan.category.key for plan in prepared.category_run_plans] == ["50021853", "216502"]
        collected = collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                            database=database, runtime_logger=logger)
        # 分类内41条，跨分类允许同商品；部分成功只剩第一分类。
        expected_items = total * (1 if partial else 2)
        expected_pages = max(1, (total + 19) // 20) * (1 if partial else 2)
        staged = CsvExporter(runtime / "exports").prepare(task_id=task.id, display_name=task.display_name,
                                                          planned_at=CAPTURED_AT, version=1,
                                                          batch_id=prepared.batch_id,
                                                          category_runs=collected.category_runs, platform="taobao")
        published = database.publish_collected_batch(collected, 1, staged, CAPTURED_AT + timedelta(minutes=1))
        prepared.storage.sync_collection_snapshot(published.snapshot)
        if repeated_product and total > 1:
            # 有重复商品时降级必须拒绝，不能丢掉排名位置来恢复旧唯一约束。
            migration_config = Config("alembic.ini")
            migration_config.set_main_option("sqlalchemy.url", str(database.engine.url))
            with pytest.raises(RuntimeError, match="repeated Taobao ranking items"):
                command.downgrade(migration_config, "0006_taobao_metrics")
        with database.session_factory() as session:
            # 正式批次从创建时即为淘宝，不允许测试后来改平台掩盖入口缺口。
            batch = session.get(CollectionBatch, prepared.batch_id)
            raw_pages = session.scalars(select(RawResponse).order_by(RawResponse.category_run_id, RawResponse.page_no)).all()
            products = session.scalars(select(ProductRankEntryModel)).all()
            # 三层身份按真实元数据保留，而非用空抖音列绕过 Manifest 根校验。
            categories = session.scalars(select(CategoryRun)).all()
            assert all(category.level1_category_id == "50025705" and category.level2_category_id == "2165"
                       for category in categories)
            assert batch.platform == "taobao"
            assert batch.status == ("partial_success" if partial else "success")
            assert batch.brand_type is None and batch.price_bin is None
            assert len(products) == expected_items and len(raw_pages) == expected_pages
            if repeated_product and total:
                assert {product.product_id for product in products} == {"100001"}
                assert all(product.platform == "taobao" for product in products)
            assert all(product.visitor_count_min_value is None and product.pay_amount_min_value is None
                       for product in products)
            for raw in raw_pages:
                # 磁盘正文、SQLite索引、实际请求页码共同证明20/20/1落盘顺序。
                with gzip.open(raw.path, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
                assert len(payload["data"]["data"]["data"]) == raw.item_count
                assert raw.item_count == max(0, min(20, total - (raw.page_no - 1) * 20))
                assert raw.safe_params["page"] == raw.page_no and raw.safe_params["pageSize"] == 20
                assert "token" not in raw.safe_params
        # Manifest 与 SQLite 正式快照一致，页数和完整成功商品数不是浏览器估计。
        manifest = json.loads(prepared.storage.manifest_path.read_text(encoding="utf-8"))
        assert manifest["platform"] == "taobao"
        assert manifest["status"] == published.snapshot.status
        assert manifest["saved_page_count"] == expected_pages
        assert manifest["collected_item_count"] == expected_items
        assert manifest["successful_category_count"] == (1 if partial else 2)
        assert manifest["failed_category_count"] == int(partial)
        with staged.final_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            assert tuple(reader.fieldnames) == TAOBAO_CSV_HEADERS
        assert len(rows) == expected_items
        if rows:
            assert rows[0]["支付买家数"] == "2.5万 ~ 5万" and rows[0]["访客数"] == ""
            assert rows[0]["商品链接"].startswith("https://sycm.taobao.com/")
        assert controls.initializations == (1 if lost_page is None else 2)
        assert len(confirmation_failures) == int(lost_page is not None)
    finally:
        adapter.close()
        database.close()
    assert closed == [True]


def test_final_integrity_failure_closes_real_database_categories_without_next_page(tmp_path, monkeypatch):
    """Actual SQLite must accept category-wide failure after the last raw page is saved."""
    # 使用真实迁移、原始落盘、数据库和Manifest，不让数据库替身掩盖空页码边界。
    adapter, controls, task, _, _ = setup(monkeypatch)
    controls.total = 1
    monkeypatch.setattr(category_batch, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    monkeypatch.setattr(category_collection, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    original = adapter.collect_scope

    def fail_after_complete_pages(*args):
        """Reject only the final whole-category integrity step, after saving all pages."""
        yield from original(*args)
        raise ResponseContractError("Synthetic complete-page duplicate", category="duplicate_rank")

    monkeypatch.setattr(adapter, "collect_scope", fail_after_complete_pages)
    runtime = tmp_path / "runtime"
    database_path = runtime / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    logger = RuntimeLogger(runtime / "logs")
    try:
        prepared = prepare_category_batch(runtime_root=runtime, batch_id="c" * 32, task=task,
                                          business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT,
                                          mode="normal", client=adapter, database=database, runtime_logger=logger)
        with pytest.raises(CategoryBatchCollectionError) as caught:
            collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                   database=database, runtime_logger=logger)
        assert caught.value.cause.category == "duplicate_rank"
        with database.session_factory() as session:
            # 失败分类仍完整保留raw进度，但不生成正式商品，也不跳过下一个分类。
            categories = session.scalars(select(CategoryRun)).all()
            assert len(categories) == 2
            assert all(category.status == "failed" and category.failed_page is None
                       and category.saved_page_count == 1 for category in categories)
            assert len(session.scalars(select(RawResponse)).all()) == 2
            assert not session.scalars(select(ProductRankEntryModel)).all()
        manifest = json.loads(prepared.storage.manifest_path.read_text())
        assert manifest["failed_category_count"] == 2
        assert all(category["failed_page"] is None for category in manifest["categories"])
    finally:
        adapter.close()
        database.close()


def test_real_pipeline_keeps_browser_failure_step_and_local_screenshot(tmp_path, monkeypatch):
    """Page diagnostics must survive shared collection wrapping and actual artifact writes."""
    # 使用真实存储边界验证截图与具体控件步骤不被通用异常包装丢弃。
    adapter, controls, task, _, _ = setup(monkeypatch)
    # 故意不产生请求的合成控件使用短响应预算，仍核验有限恢复后的真实落盘诊断。
    adapter.settings.response_timeout_seconds = 0.01
    monkeypatch.setattr(category_batch, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    monkeypatch.setattr(category_collection, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))

    def fail_select(*args):
        """Fail as a real page control would, without including the underlying URL."""
        raise BrowserOperationError("Safe page failure", category="browser_page_error",
                                    failed_step="taobao_category_select", exception_type="TimeoutError",
                                    screenshot=b"synthetic-local-screenshot")

    monkeypatch.setattr(controls, "select_scope", fail_select)
    runtime = tmp_path / "runtime"
    database_path = runtime / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    logger = RuntimeLogger(runtime / "logs")
    try:
        prepared = prepare_category_batch(runtime_root=runtime, batch_id="d" * 32, task=task,
                                          business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT,
                                          mode="normal", client=adapter, database=database, runtime_logger=logger)
        with pytest.raises(CategoryBatchCollectionError):
            collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                   database=database, runtime_logger=logger)
        assert (prepared.storage.artifact_dir / "failure.png").read_bytes() == b"synthetic-local-screenshot"
        summary = json.loads((prepared.storage.artifact_dir / "failure.json").read_text())
        assert summary["failed_step"] == "taobao_category_select"
        assert summary["exception_type"] == "TimeoutError" and summary["screenshot_saved"] is True
    finally:
        adapter.close()
        database.close()


def test_all_business_failures_close_actual_batch_without_official_products(tmp_path, monkeypatch):
    """An all-failed Taobao task cannot be turned into an accepted empty ranking."""
    # 使用同一生产适配器和共享编排，仅替换页面发出的合成响应。
    adapter, controls, task, scope, closed = setup(monkeypatch)
    monkeypatch.setattr(category_batch, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))
    monkeypatch.setattr(category_collection, "datetime", SimpleNamespace(now=lambda zone: CAPTURED_AT))

    def fail_rank(page):
        """Emit a matched but unsuccessful business response for every category."""
        from compass_collector.platforms.taobao_product_rank import build_expected_params
        # unknown code 不预先映射登录或权限含义，保留真实错误类别。
        params = build_expected_params(task, controls.scope, CAPTURED_AT.date(), controls.page_no)
        controls.emit(Response(Request(page, params), {"code": 12345, "message": "synthetic failure"}))

    monkeypatch.setattr(controls, "rank", fail_rank)
    runtime = tmp_path / "runtime"
    database_path = runtime / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    logger = RuntimeLogger(runtime / "logs")
    try:
        prepared = prepare_category_batch(runtime_root=runtime, batch_id="b" * 32, task=task,
                                          business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT,
                                          mode="normal", client=adapter, database=database, runtime_logger=logger)
        with pytest.raises(CategoryBatchCollectionError) as failure:
            collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                   database=database, runtime_logger=logger)
        assert failure.value.cause.category == "taobao_business_error"
        with database.session_factory() as session:
            # 未执行分类全部收口，失败不能残留 running 或伪造 published_at。
            batch = session.get(CollectionBatch, prepared.batch_id)
            categories = session.scalars(select(CategoryRun)).all()
            assert batch.status == "failed" and batch.published_at is None
            assert all(category.status == "failed" for category in categories)
            assert not session.scalars(select(ProductRankEntryModel)).all()
            assert not session.scalars(select(RawResponse)).all()
        manifest = json.loads(prepared.storage.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"
        assert manifest["failed_category_count"] == 2
        assert manifest["not_started_category_count"] == 0
        assert not list(runtime.rglob("*.csv"))
        # 失败正文留在每分类受限材料中，不进入 Manifest 或运行日志。
        bodies = list(prepared.storage.artifact_dir.rglob("failure-response.txt"))
        assert len(bodies) == 2
        assert all(json.loads(body.read_text(encoding="utf-8"))["code"] == 12345 for body in bodies)
        assert "synthetic failure" not in prepared.storage.manifest_path.read_text(encoding="utf-8")
        assert all("synthetic failure" not in log.read_text(encoding="utf-8")
                   for log in (runtime / "logs").rglob("*.jsonl"))
    finally:
        adapter.close()
        database.close()
    assert closed == [True]
