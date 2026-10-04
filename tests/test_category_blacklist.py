"""分类黑名单的平台隔离、路径匹配和真实 PostgreSQL 编排回归。"""

import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from pg_support import pg_url
from compass_collector.config import PlatformConfig, load_config
from compass_collector.errors import CategoryBatchPreparationError
from compass_collector.models import CategoryDiscoveryResult, DiscoveredScope
from compass_collector.persistence import CategoryRun, CollectionBatch, Database, upgrade_database
from compass_collector.platforms.contracts import DiscoveryCapture, PageCapture
from compass_collector.runner import TaskExecutionPlan, collect_task
from compass_collector.runtime_logging import RuntimeLogger


def test_blacklist_normalizes_names_without_merging_platforms():
    """名称规范化不跨平台共享，不改写分类中的标点或内部空格。"""
    # 使用真实应用配置验证平台筛选后仍保留各自的名单。
    config = load_config(Path("config/tasks.yaml"))
    config.platforms["taobao"] = PlatformConfig(
        profile_dir="runtime/taobao-browser-profile",
        category_blacklist=[" 其他 ", "其他", "头发清洁/护理/造型", "内 部"],
    )
    assert config.for_platform("taobao").platforms["taobao"].category_blacklist == [
        "其他", "头发清洁/护理/造型", "内 部",
    ]
    assert config.for_platform("compass").platforms["compass"].category_blacklist == []


@pytest.mark.parametrize("invalid", [[""], [" \t"], [123], [None], "其他"])
def test_blacklist_rejects_invalid_names(invalid):
    """不把空名称、数字、空值或整段字符串静默转换成可运行配置。"""
    with pytest.raises(ValidationError):
        PlatformConfig(profile_dir="runtime/test-profile", category_blacklist=invalid)


class BlacklistAdapter:
    """在平台适配器边界提供合成分类，真实执行共享采集与落库逻辑。"""

    def __init__(self):
        """准备跨父级同名叶节点、父级命中和近似但不相同的名称。"""
        # 不同路径下两个“其他”都应过滤，“其他用品”应保留。
        self.paths = (
            ("行业", "私处护理", "洗液"),
            ("行业", "分支甲", "其他"),
            ("行业", "分支乙", " 其他 "),
            ("行业", "分支甲", "其他用品"),
            ("行业", "分支乙", "清洁剂"),
        )
        # 记录实际进入榜单采集的名称，能发现过滤后仍采集或重复补采的问题。
        self.collected = []
        # 完整分类树证据必须保留过滤前的全部路径。
        self.payload = {"synthetic_paths": [list(path) for path in self.paths]}

    def discover_scopes(self, task):
        """返回规范化分类，不触碰真实站点、账号或浏览器。"""
        return DiscoveryCapture(
            CategoryDiscoveryResult(None, None, tuple(
                DiscoveredScope(
                    index, str(index), path,
                    {"industry_id": "100", "level2_id": str(200 + index)},
                )
                for index, path in enumerate(self.paths, start=1)
            )),
            self.payload,
        )

    def collect_scope(self, task, scope, business_date):
        """提供已验证的空榜页，证明仅保留分类会进入分页持久化。"""
        self.collected.append(scope.path)
        yield PageCapture(
            1, 0, 1, datetime.now(ZoneInfo("Asia/Shanghai")), (),
            {"synthetic_empty_ranking": True}, {},
        )


@pytest.mark.parametrize("platform", ["compass", "taobao"])
@pytest.mark.parametrize("block_all", [False, True])
def test_runner_blacklist_filters_before_collection_and_persistence(
    tmp_path, monkeypatch, platform, block_all,
):
    """真实 Runner 证明平台隔离、计数连续、审计完整和全过滤终态。"""
    # 只给当前测试创建独立 schema，不连接任何业务数据库。
    database_url = pg_url(tmp_path)
    upgrade_database(database_url)
    # 测试运行目录、时刻、平台配置和事件列表均只属于本次用例。
    runtime = tmp_path / "runtime"
    monkeypatch.setattr("compass_collector.runner.RUNTIME_ROOT", runtime)
    config = load_config(Path("config/tasks.yaml"))
    config.platforms["taobao"].category_blacklist = ["私处护理", "其他"]
    if block_all:
        config.platforms[platform].category_blacklist = ["行业"]
    # 同一个应用配置含两个平台，入口必须按任务平台读取名单。
    task = next(task for task in config.tasks if task.platform == platform)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    plan = TaskExecutionPlan(task=task, business_date=now.date(), planned_at=now, version=1)
    adapter = BlacklistAdapter()
    events = []
    logger = RuntimeLogger(runtime / "logs", event_sink=events.append, platform=platform)
    database = Database(database_url)
    try:
        if block_all:
            with pytest.raises(CategoryBatchPreparationError) as caught:
                collect_task(plan, config, adapter, logger, "blacklist-test", database=database, mode="dry_run")
            assert caught.value.cause.category == "category_blacklist_empty"
        else:
            # 不伪造数据库快照，完成真实分类分页保存和 dry-run 终态。
            collected = collect_task(plan, config, adapter, logger, "blacklist-test", database=database, mode="dry_run")
            snapshot = database.finalize_dry_run(collected, datetime.now(ZoneInfo("Asia/Shanghai")))
            collected.storage.sync_collection_snapshot(snapshot)
        with database.session_factory() as session:
            # SQL 和 Manifest 应只包含待采分类，排除项不产生失败计数。
            batch = session.get(CollectionBatch, "blacklist-test")
            rows = session.scalars(select(CategoryRun).order_by(CategoryRun.discovery_order)).all()
            assert batch.status == ("failed" if block_all else "success")
            assert batch.published_at is None
            assert batch.failed_category_count == 0
            assert batch.config_snapshot["category_blacklist"] == config.platforms[platform].category_blacklist
            if block_all:
                assert batch.error_category == "category_blacklist_empty"
            # 被过滤的分类路径不应进入数据库或采集适配器。
            expected = [] if block_all else list(adapter.paths[3:] if platform == "taobao" else adapter.paths)
            assert adapter.collected == expected
            assert [row.category_name for row in rows] == [path[-1] for path in expected]
            assert [row.discovery_order for row in rows] == list(range(1, len(expected) + 1))
            assert batch.discovered_category_count == len(expected)
            # 原始分类树仍包含过滤前全部分类。
            with gzip.open(batch.category_tree_raw_path, "rt") as raw_file:
                assert json.load(raw_file) == adapter.payload
            # 唯一 Manifest 应与数据库权威计数、配置快照一致。
            manifest = json.loads(Path(batch.manifest_path).read_text())
            assert manifest["discovered_category_count"] == len(expected)
            assert manifest["config_snapshot"] == batch.config_snapshot
            assert len(manifest["categories"]) == len(expected)
        # 黑名单日志保留每个排除项的路径，且不伪造 category_run_id。
        skipped = [event for event in events if event["event"] == "category_blacklisted"]
        assert len(skipped) == len(adapter.paths) - len(expected)
        assert all(event["platform"] == platform for event in skipped)
    finally:
        database.close()
