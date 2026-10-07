"""远程分类集成：真实隔离 PostgreSQL，替换网络与浏览器响应边界。"""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from pg_support import pg_url

from compass_collector.category_batch import prepare_category_batch
from compass_collector.category_catalog import catalog_from_payload, scope_path
from compass_collector.category_rules import preview_rules
from compass_collector.config import load_config
from compass_collector.continuation import continuation_task
from compass_collector.errors import CategoryBatchPreparationError, CollectorError
from compass_collector.persistence import CollectionBatch, Database, upgrade_database
from compass_collector.platforms.contracts import DiscoveryCapture
from compass_collector.runtime_logging import RuntimeLogger
import compass_collector.remote_categories as remote


class CatalogAdapter:
    """记录是否访问分类浏览器，禁止商品请求。"""

    def __init__(self, payload):
        """响应是脱敏 fixture，不包含真实平台账户。"""
        self.payload = payload
        self.calls = 0

    def discover_scopes(self, task, *, full_catalog=False):
        """远程范围必须从完整目录生成，不能先应用 YAML。"""
        assert full_catalog
        self.calls += 1
        # 复用真实目录解析，网络之外的代码不替换。
        _, discovery = catalog_from_payload(task.platform, self.payload)
        return DiscoveryCapture(discovery, self.payload)


def scenario(tmp_path):
    """准备真实迁移数据库、完整目录及与旧行业不同的远程任务范围。"""
    # 本机配置仅负责其他采集参数，远程范围刻意覆盖另一行业。
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    # 独立目录和数据库使测试不操作仓库 runtime。
    database_url = pg_url(tmp_path)
    upgrade_database(database_url)
    database = Database(database_url)
    # 源响应与源顺序沿用已有解析器 fixture。
    payload = json.loads(Path("tests/fixtures/category_tree.json").read_text())
    nodes, _ = catalog_from_payload("compass", payload)
    # 选取有多个三级节点的行业，排除其中一项。
    target = ["13"]
    leaves = [node["path"] for node in nodes if len(node["path"]) == 3 and node["path"][0] == "13"]
    rules = {"tasks": [{"id": task.id, "display_name": task.display_name, "targets": [target]}],
             "exclusions": [leaves[0]], "unresolved_names": []}
    # 模拟服务端已发布状态，版本必须写入批次快照。
    response = {"schema_version": 1, "platform": "compass", "revision": 3, "published": True, "rules": rules}
    # 北京时间沿用当前采集业务日期规则。
    planned = datetime.now(ZoneInfo("Asia/Shanghai"))
    # 阶段二仅准备分类，没有商品分页。
    arguments = dict(runtime_root=tmp_path, batch_id=uuid4().hex, task=task,
        business_date=planned.date(), planned_at=planned, mode="force",
        client=CatalogAdapter(payload), database=database, runtime_logger=RuntimeLogger(tmp_path / "logs"),
        category_config_source="remote", category_blacklist=("食品饮料",))
    return arguments, response, nodes


def test_remote_plan_snapshot_and_legacy_recovery(tmp_path, monkeypatch):
    """远程计划忽略 YAML 黑名单，数据库与 Manifest 清单完全一致。"""
    # 真正执行目录展开和数据库写入，仅替换 HTTP 边界。
    arguments, response, nodes = scenario(tmp_path)
    monkeypatch.setattr(remote, "remote_request", lambda platform, suffix="", body=None: response if not suffix else {"catalog_version": "synced"})
    try:
        result = prepare_category_batch(**arguments)
        expected = [item["path"] for item in preview_rules(nodes, response["rules"], arguments["task"].id)["included"]]
        assert [scope_path(item) for item in result.discovery.categories] == expected
        assert arguments["client"].calls == 1
        assert not (tmp_path / "category-catalogs/compass/pending.json").exists()
        assert (result.storage.batch_dir / "category-catalog.json").is_file()
        # 每次数据库重新加载 JSON，检查的是已持久化结果。
        with arguments["database"].session_factory() as session:
            snapshot = session.get(CollectionBatch, arguments["batch_id"])
            assert snapshot.config_snapshot["remote_category_config"]["revision"] == 3
            assert snapshot.config_snapshot["remote_category_config"]["effective_paths"] == expected
            assert snapshot.config_snapshot["category_blacklist"] == []
            assert continuation_task(snapshot).id == arguments["task"].id
            assert result.storage.manifest["config_snapshot"] == snapshot.config_snapshot
    finally:
        arguments["database"].close()


def test_remote_failure_stops_before_browser(tmp_path, monkeypatch):
    """配置服务失败必须记录失败，不回退 YAML，也不打开平台浏览器。"""
    arguments, _, _ = scenario(tmp_path)
    def unavailable(*args, **kwargs):
        """模拟 HTTP 边界失败，保证没有任何旧规则 fallback。"""
        raise CollectorError("不可用", category="category_remote_unavailable")
    monkeypatch.setattr(remote, "remote_request", unavailable)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error:
            prepare_category_batch(**arguments)
        assert error.value.cause.category == "category_remote_unavailable"
        assert arguments["client"].calls == 0
        with arguments["database"].session_factory() as session:
            snapshot = session.get(CollectionBatch, arguments["batch_id"])
            assert snapshot.status == "failed"
            assert snapshot.discovered_category_count == 0
    finally:
        arguments["database"].close()


def test_upload_pending_and_missing_rule(tmp_path, monkeypatch):
    """目录上传失败可采集；活动规则失效则保留目录并阻止采集。"""
    arguments, response, _ = scenario(tmp_path)
    def requests(platform, suffix="", body=None):
        """读取可用但上传失败，模拟不同接口的独立失败。"""
        if suffix:
            raise CollectorError("同步不可用", category="category_remote_unavailable")
        return response
    monkeypatch.setattr(remote, "remote_request", requests)
    try:
        result = prepare_category_batch(**arguments)
        assert result.discovery.categories
        assert (tmp_path / "category-catalogs/compass/pending.json").is_file()
        # 新批次规则引用已消失的三级节点，应失败而不是忽略排除。
        response["rules"]["exclusions"] = [["13", "fixture-level2-snacks", "missing"]]
        arguments["batch_id"] = uuid4().hex
        with pytest.raises(CategoryBatchPreparationError) as error:
            prepare_category_batch(**arguments)
        assert error.value.cause.category == "category_remote_scope_invalid"
    finally:
        arguments["database"].close()


def test_unpublished_config_is_not_executable(tmp_path, monkeypatch):
    """草稿导入不是发布，新采集不能使用未经网页确认的规则。"""
    arguments, response, _ = scenario(tmp_path)
    response["published"] = False
    monkeypatch.setattr(remote, "remote_request", lambda *args, **kwargs: response)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error:
            prepare_category_batch(**arguments)
        assert error.value.cause.category == "category_remote_invalid"
        assert arguments["client"].calls == 0
    finally:
        arguments["database"].close()
