"""淘宝/天猫分组通过真实批次、限页、原始材料和发布读回验证。"""

import csv
from pathlib import Path
from datetime import timedelta

import pytest
from pydantic import ValidationError

from pg_support import pg_url
from compass_collector import category_batch, category_collection, continuation, persistence, remote_categories
from compass_collector.category_batch import prepare_category_batch
from compass_collector.category_collection import collect_category_batch
from compass_collector.config import load_config
from compass_collector.exporter import CsvExporter
from compass_collector.errors import HttpRequestError
from compass_collector.persistence import Database, upgrade_database
from compass_collector.platforms.taobao_config import TaobaoFiltersConfig
from compass_collector.ranking_sync import build_sync_payload
from compass_collector.runtime_logging import RuntimeLogger
from test_continuation import FrozenDateTime
from test_taobao_adapter import setup
from test_taobao_product_rank import CAPTURED_AT


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("total", [0, 41, 81])
@pytest.mark.parametrize("retry", [False, True])
def test_seller_groups_share_frozen_pages_and_preserve_publication(tmp_path, monkeypatch, single, total, retry):
    """实际采集→落盘→续采→CSV→同步，覆盖单分类切换和两组排名身份。"""
    # 浏览器运输为合成事件，其余分类规则、页契约和存储走生产实现。
    adapter, controls, task, _, _ = setup(monkeypatch)
    controls.total = total
    task = task.model_copy(update={"filters": TaobaoFiltersConfig(seller_platforms=("taobao", "tmall"))})
    # 只让天猫分页失败一次，验证补采不会重写淘宝的原始材料。
    failures = []
    # 保存控件原行为，异常后仍经生产分类补采入口恢复。
    original_next = controls.next_page

    def fail_tmall_once(page):
        """在已保存第一页后失败一次，随后真实执行原合成控件事件。"""
        if retry and controls.seller_type == "9" and not failures:
            failures.append(True)
            raise HttpRequestError("Synthetic tmall page failure", category="page_response_timeout")
        return original_next(page)

    monkeypatch.setattr(controls, "next_page", fail_tmall_once)
    # 同一个淘宝远程版本同时固定范围及限页，绝不请求独立天猫配置。
    targets = [["50025705", "2165", "50021853"]] if single else [["50025705"]]
    remote = {"schema_version": 1, "platform": "taobao", "revision": 23,
              "rules": {"tasks": [{"id": task.id, "display_name": task.display_name, "targets": targets}],
                        "exclusions": [], "unresolved_names": []},
              "collection_limits": {"max_pages_per_category": 2}}
    # 配置读取审计用于确认一批次只读取淘宝一次。
    fetches = []

    def fetch(platform, task_id):
        """只替换HTTP边界，规则实际经过生产目录预览和展开。"""
        fetches.append((platform, task_id))
        return remote

    monkeypatch.setattr(remote_categories, "fetch_category_config", fetch)
    monkeypatch.setattr(remote_categories, "retry_catalog", lambda *args: None)
    monkeypatch.setattr(remote_categories, "upload_catalog", lambda *args: None)
    # 数据库跨天保护也使用同一固定日期，不能依赖运行测试当天。
    for module in (category_batch, category_collection, continuation, persistence):
        monkeypatch.setattr(module, "datetime", FrozenDateTime)
    # 临时SQLite库实际迁移，不接触已有采集数据。
    url = pg_url(tmp_path)
    upgrade_database(url, platform="taobao")
    database = Database(url)
    # raw、Manifest、日志及CSV均属于当前测试目录。
    root = tmp_path / "runtime"
    logger = RuntimeLogger(root / "logs", platform="taobao")
    try:
        # 批次固定两组分类，先淘宝的全部分类再天猫的全部分类。
        prepared = prepare_category_batch(runtime_root=root, batch_id="c" * 32, task=task,
            business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT, mode="force",
            client=adapter, database=database, runtime_logger=logger, category_config_source="remote")
        # 原始三级ID不受分组执行键影响，远程规则仍匹配真实分类路径。
        count = 1 if single else 2
        assert [scope.platform_metadata["seller_platform"] for scope in prepared.discovery.categories] == ["taobao"] * count + ["tmall"] * count
        assert len({scope.key for scope in prepared.discovery.categories}) == count * 2
        assert all(scope.platform_metadata["cate_id"] == scope.key.split(":")[1] for scope in prepared.discovery.categories)
        # 完整生产循环应按每组2页限额完成，不取真实总数的剩余页。
        collected = collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                            database=database, runtime_logger=logger)
        assert [run.plan.category.key for run in collected.category_runs] == [scope.key for scope in prepared.discovery.categories]
        assert all(len(run.entries) == min(total, 40) for run in collected.category_runs)
        assert all([record.safe_params["sellerType"] for record in run.raw_pages] ==
                   [int(run.plan.category.platform_metadata["seller_type"])] * len(run.raw_pages)
                   for run in collected.category_runs)
        assert all(len(run.raw_pages) == (1 if total == 0 else 2) for run in collected.category_runs)
        assert failures == ([True] if retry and total else [])
        # 续采从持久化材料校验并恢复两组，不读取已变化的远程页数。
        snapshot = database.collection_snapshot(prepared.batch_id)
        remote["collection_limits"]["max_pages_per_category"] = 5
        restored, completed = continuation.prepare_continuation(snapshot,
            continuation.continuation_task(snapshot), database, root, logger)
        assert len(completed) == count * 2
        assert restored.storage.manifest["config_snapshot"]["remote_category_config"]["collection_limits"]["max_pages_per_category"] == 2
        assert fetches == [("taobao", task.id)]
        # 同一产品的两组排名发布后仍独立存在，CSV明确显示店铺分组。
        staged = CsvExporter(root / "exports").prepare(task_id=task.id, display_name=task.display_name,
            planned_at=CAPTURED_AT, version=1, batch_id=prepared.batch_id,
            category_runs=collected.category_runs, platform="taobao")
        database.publish_collected_batch(collected, 1, staged, CAPTURED_AT + timedelta(minutes=1))
        monkeypatch.setenv("TAOBAO_DATABASE_URL", url.render_as_string(hide_password=False))
        # API同步从正式库读回，分类展示必须与公开CSV逐行一致。
        payload = build_sync_payload({"platform": "taobao", "task_id": task.id,
            "batch_id": prepared.batch_id, "business_date": CAPTURED_AT.date().isoformat(),
            "item_count": min(total, 40) * count * 2, "shops_schema_version": 1})
        with staged.final_path.open(encoding="utf-8-sig", newline="") as handle:
            # 分类前缀由生产模型导出，不能在测试中手工拼接结果。
            rows = list(csv.DictReader(handle))
        assert [row["category"] for row in payload["rows"]] == [row["分类"] for row in rows]
        assert payload["collection_config"]["collection_limits"]["max_pages_per_category"] == 2
        if total:
            assert rows[0]["分类"].startswith("淘宝 · ") and rows[-1]["分类"].startswith("天猫 · ")
            assert len({row["category_run_id"] for row in payload["rows"]}) == count * 2
    finally:
        adapter.close()
        database.close()


@pytest.mark.parametrize("path", ["config/tasks.yaml", "config/taobao.yaml", "config/taobao-poc.yaml"])
def test_all_taobao_entry_configs_enable_ordered_groups(path):
    """三个真实入口启用相同规则，旧快照缺少字段时保持旧条件。"""
    assert all(task.filters.seller_platforms == ("taobao", "tmall")
               for task in load_config(Path(path)).tasks if task.platform == "taobao")
    assert TaobaoFiltersConfig(seller_type=-1).seller_platforms == ()


@pytest.mark.parametrize("platforms", [("tmall", "taobao"), ("taobao",), ("taobao", "taobao"), ("all",)])
def test_invalid_seller_group_order_is_rejected(platforms):
    """重复、颠倒或全部不能进入新的分组配置。"""
    with pytest.raises(ValidationError):
        TaobaoFiltersConfig(seller_platforms=platforms)
