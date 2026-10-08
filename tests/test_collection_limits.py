"""限页请求、完成统计和续采使用隔离合成数据，绝不访问真实账号。"""
from datetime import datetime
from pathlib import Path
import pytest
from pg_support import pg_config
from compass_collector.config import load_config, CollectionConfig
from compass_collector.collection_limits import pagination_counts, page_limit
from compass_collector.platforms import compass, taobao
from compass_collector.category_batch import prepare_category_batch
from compass_collector.category_collection import _collect_category_run
from compass_collector import category_batch, category_collection, continuation, persistence
from compass_collector.persistence import Database, upgrade_database
from compass_collector.runtime_logging import RuntimeLogger
from test_taobao_adapter import setup
from test_taobao_product_rank import CAPTURED_AT
from test_category_collection import build_page_payload, build_category
from test_continuation import FrozenDateTime


@pytest.mark.parametrize("total", [0, 1, 20, 21, 60, 61, 81])
def test_taobao_adapter_requests_only_plan(monkeypatch, total):
    """生产淘宝适配器不请求第四页，尾页仍按真实条数校验。"""
    adapter, controls, task, scope, _ = setup(monkeypatch)
    controls.total = total
    task = task.model_copy(update={"max_pages_per_category": 3})
    try:
        pages = list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
        expected_pages, expected_items = pagination_counts(total, 20, 3)
        assert len(pages) == expected_pages
        assert sum(len(page.entries) for page in pages) == expected_items
        assert all(page.api_total == total for page in pages)
        assert controls.page_no == expected_pages
    finally:
        adapter.close()


@pytest.mark.parametrize("total", [0, 1, 20, 21, 30, 31, 81])
def test_compass_adapter_requests_only_plan(monkeypatch, total):
    """生产抖音适配器限三页、保持排名与原始 total。"""
    monkeypatch.setattr(compass, "datetime", FrozenDateTime)
    task = load_config(Path("config/tasks.yaml")).for_platform("compass").tasks[0].model_copy(update={"max_pages_per_category": 3})
    scope = build_category(1)
    adapter = compass.CompassAdapter.__new__(compass.CompassAdapter)
    adapter.settings = CollectionConfig()
    requested = []
    def capture(task, scope, day, number, **kwargs):
        """只有浏览器响应合成，生产解析和完整性判断仍照常运行。"""
        requested.append(number)
        return build_page_payload(category_id=scope.category_id, page_no=number, total=total), {}
    monkeypatch.setattr(adapter, "_capture", capture)
    monkeypatch.setattr(adapter, "_payload", lambda value: value)
    monkeypatch.setattr(adapter, "_settle_ranking", lambda: None)
    monkeypatch.setattr(adapter, "_confirm_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_pump", lambda *args: None)
    pages = list(adapter.collect_scope(task, scope, CAPTURED_AT.date()))
    expected_pages, expected_items = pagination_counts(total, 10, 3)
    assert requested == list(range(1, expected_pages + 1))
    assert sum(len(page.entries) for page in pages) == expected_items
    assert all(page.api_total == total for page in pages)


def test_limited_success_persistence_and_continuation(tmp_path, monkeypatch):
    """真实 PostgreSQL 保存 81 总数、60 计划；续采跳过完整三页且保留配置。"""
    adapter, controls, task, _, _ = setup(monkeypatch)
    controls.total = 81
    for module in (category_batch, category_collection, continuation, persistence):
        monkeypatch.setattr(module, "datetime", FrozenDateTime)
    config = pg_config(load_config(Path("config/taobao.yaml")), tmp_path).for_platform("taobao")
    upgrade_database(config.database.url, platform="taobao")
    database = Database(config.database.url)
    root = tmp_path / "runtime"
    logger = RuntimeLogger(root / "logs", platform="taobao")
    # 在新批次创建边界注入固定规则，不能绕过“分类创建后禁止改配置”。
    create = database.create_batch
    def create_frozen_batch(**values):
        """替换远程网络边界，实际批次仍由生产事务创建。"""
        values["config_snapshot"]["remote_category_config"] = {"revision": 1, "collection_limits": {"max_pages_per_category": 3}}
        return create(**values)
    monkeypatch.setattr(database, "create_batch", create_frozen_batch)
    try:
        prepared = prepare_category_batch(runtime_root=root, batch_id="d" * 32, task=task,
            business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT, mode="force", client=adapter,
            database=database, runtime_logger=logger)
        run = _collect_category_run(prepared_batch=prepared, task=task, plan=prepared.category_run_plans[0],
            client=adapter, database=database, runtime_logger=logger, control=None)
        state = database.collection_snapshot(prepared.batch_id)
        assert (state.categories[0].api_total, state.categories[0].planned_item_count,
                state.categories[0].saved_item_count, state.categories[0].status) == (81, 60, 60, "success")
        assert len(run.entries) == 60 and len(run.raw_pages) == 3
        restored, completed = continuation.prepare_continuation(state, continuation.continuation_task(state), database, root, logger)
        assert len(completed) == 1 and len(completed[0].entries) == 60
        assert page_limit(restored.storage.manifest["config_snapshot"]) == 3
    finally:
        adapter.close()
        database.close()


@pytest.mark.parametrize("tmall", [True, False, None, "true", 1])
def test_shop_logo_and_strict_tmall(tmall):
    """店铺来源图片规范化，天猫仅认严格 bool，坏 logo 不丢商品。"""
    from test_taobao_product_rank import page_payload
    from compass_collector.platforms import taobao_product_rank, compass_product_rank
    payload = page_payload(total=1)
    payload["data"]["data"]["data"][0]["shop"].update(pictureUrl="//img.example/shop.jpg", b2CShop=tmall)
    entry = taobao_product_rank.parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT)[0]
    assert entry.shops[0].image_url == "https://img.example/shop.jpg"
    assert entry.shops[0].is_tmall is (tmall if type(tmall) is bool else None)
    payload["data"]["data"]["data"][0]["shop"]["pictureUrl"] = "javascript:invalid"
    assert taobao_product_rank.parse_page_entries(payload, page_no=1, captured_at=CAPTURED_AT)[0].shops[0].image_url is None
    compass_payload = build_page_payload(category_id="synthetic", page_no=1, total=1)
    # 原始数组顺序和多个店铺必须保留，一张坏图片不影响完整商品。
    shops = compass_payload["data"]["data_result"][0]["product_info"]["shop_list"]
    shops[0]["image"] = "//img.example/first.jpg"
    shops.append({"shop_id": "second", "shop_name": "第二店铺", "image": 42})
    result = compass_product_rank.parse_page_entries(compass_payload, page_no=1, captured_at=CAPTURED_AT)[0]
    assert [shop.image_url for shop in result.shops] == ["https://img.example/first.jpg", None]
    assert [shop.position for shop in result.shops] == [0, 1]
