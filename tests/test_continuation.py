"""Exercise restart, publication and platform flags using isolated PostgreSQL evidence."""

import csv
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pg_support import pg_config
from compass_collector import category_batch, category_collection, continuation, persistence, runner
from compass_collector.category_batch import prepare_category_batch
from compass_collector.category_collection import _collect_category_run
from compass_collector.cli import build_parser
from compass_collector.config import load_config
from compass_collector.models import RawPageRecord
from compass_collector.persistence import CollectionBatch, Database, ProductRankEntryModel, upgrade_database
from compass_collector.platforms import taobao
import adapter_fixtures
from adapter_fixtures import FixtureResponse
from test_runner_dynamic import FakeCompassClient, build_rank_payload
from compass_collector.runtime_logging import RuntimeLogger
from test_taobao_adapter import setup
from test_taobao_product_rank import CAPTURED_AT


class FrozenDateTime(datetime):
    """Keep runner, storage and platform on the same deterministic business day."""

    @classmethod
    def now(cls, tz=None):
        """返回固定的北京时间，不依赖运行测试当天。"""
        return CAPTURED_AT if tz else CAPTURED_AT.replace(tzinfo=None)


class ContinuationCompassClient(FakeCompassClient):
    """用现有抖音解析器提供两个分类和三页数据，覆盖与淘宝不同的分页契约。"""

    def __init__(self):
        """只构造测试会话，不访问真实抖音账号。"""
        super().__init__()
        # runner 通过会话边界清理资源；测试没有真实浏览器。
        self.session = SimpleNamespace(close=lambda: None)

    def open_session(self, **kwargs):
        """与发布重试测试共享同一可观察的浏览器入口。"""
        return self.session

    def discover_scopes(self, task):
        """冻结两个测试分类，便于分别验证跳过和重采。"""
        # 仍通过生产分类树解析器读取原始 Fixture。
        capture = super().discover_scopes(task)
        return replace(capture, discovery=replace(capture.discovery, categories=capture.discovery.categories[:2]))

    def get_product_rank_page(self, task, params):
        """模拟抖音每页10条，而不是淘宝每页20条。"""
        # 分类身份保留二三级级联参数，商品在各分类内拥有独立 ID。
        category_id = str(params["category_id"]).split(",")[-1]
        # 三页的排名必须完整连续，最后一页只有一条。
        page_no = int(params["page_no"])
        self.ranking_calls.append((category_id, page_no))
        # 沿用已有脱敏商品字段和金额/销量单位，恢复后仍由生产解析器换算。
        payload = build_rank_payload(category_id)
        # 每一行都独立复制，避免修改排名时污染其他行。
        template = payload["data"]["data_result"][0]
        payload["data"]["data_result"] = []
        for rank in range((page_no - 1) * 10 + 1, min(page_no * 10, 21) + 1):
            # 商品 ID 与排名都必须在当前分类内唯一。
            item = deepcopy(template)
            item["product_info"].update(id=f"{category_id}-product-{rank}", rank=rank)
            payload["data"]["data_result"].append(item)
        payload["data"]["page_result"].update(page_no=page_no, total=21)
        return FixtureResponse(payload=payload, body=b"sanitized", status_code=200)


@pytest.fixture(params=("taobao", "compass"))
def interrupted_batch(tmp_path, monkeypatch, request):
    """建立一个成功分类和一个仅保存首页的 running 分类，等价于被强杀后的落盘状态。"""
    # 真实解析、分页监听和数据库落盘，只有浏览器响应是合成的。
    # 同一补录契约分别运行在两平台的真实解析和数据库发布链路上。
    platform = request.param
    if platform == "taobao":
        adapter, controls, task, _, _ = setup(monkeypatch)
        controls.total = 21
    else:
        adapter = ContinuationCompassClient()
        task = load_config(Path("config/tasks.yaml")).for_platform(platform).tasks[0]
    for module in (category_batch, category_collection, continuation, persistence, runner, taobao, adapter_fixtures):
        monkeypatch.setattr(module, "datetime", FrozenDateTime)
    # 测试配置只连接每次创建的隔离 schema，不读取真实采集库。
    config = pg_config(load_config(Path("config/taobao.yaml" if platform == "taobao" else "config/tasks.yaml")), tmp_path).for_platform(platform)
    config = config.model_copy(update={"tasks": [task], "browser": config.browser.model_copy(
        update={"keep_open_after_manual_run": False})})
    # 所有 raw、Manifest 和 CSV 进入 pytest 临时目录。
    root = tmp_path / "runtime"
    monkeypatch.setattr(runner, "RUNTIME_ROOT", root)
    upgrade_database(config.database.url, platform=platform)
    # 固定批次身份验证补录不创建新批次。
    database = Database(config.database.url)
    logger = RuntimeLogger(root / "logs", platform=platform)
    prepared = prepare_category_batch(runtime_root=root, batch_id="a" * 32, task=task,
        business_date=CAPTURED_AT.date(), planned_at=CAPTURED_AT, mode="force", client=adapter,
        database=database, runtime_logger=logger)
    _collect_category_run(prepared_batch=prepared, task=task, plan=prepared.category_run_plans[0],
        client=adapter, database=database, runtime_logger=logger, control=None)
    # 第二分类只提交一页，然后直接丢弃采集器，保留 running 而不执行中止收口。
    pending = prepared.category_run_plans[1]
    database.start_category_run(pending.category_run_id, CAPTURED_AT)
    iterator = adapter.collect_scope(task, pending.category, CAPTURED_AT.date())
    page = next(iterator)
    path = prepared.storage.write_category_page(pending.category_run_id, 1, page.payload)
    snapshot = database.record_category_page(category_run_id=pending.category_run_id,
        raw_page=RawPageRecord(1, path, len(page.entries), page.captured_at, page.safe_params),
        api_total=page.api_total, target_page_count=page.target_page_count)
    prepared.storage.sync_collection_snapshot(snapshot)
    iterator.close()
    adapter.close()
    # 新会话记录实际请求的分类和发现次数；不访问真实平台、OSS 或钉钉。
    if platform == "taobao":
        fresh, fresh_controls, _, _, _ = setup(monkeypatch)
        fresh_controls.total = 21
    else:
        fresh = ContinuationCompassClient()
    calls = []
    original_collect = fresh.collect_scope

    def record_scope(selected_task, scope, day):
        """记录真实适配器调用，证明成功分类没有发起榜单请求。"""
        calls.append(scope.key)
        yield from original_collect(selected_task, scope, day)

    monkeypatch.setattr(fresh, "collect_scope", record_scope)
    monkeypatch.setattr(runner, "create_adapter", lambda *args, **kwargs: fresh)
    monkeypatch.setattr(runner, "deliver_batch_notification", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_publish_website_after_collection", lambda **kwargs: None)
    monkeypatch.setenv("OSS_ENABLED", "false")
    try:
        yield config, database, prepared, fresh, calls, root, logger
    finally:
        fresh.close()
        database.close()


def _run(config):
    """通过用户的生产 runner 入口执行正式补录。"""
    return runner.run_collection(config, config.tasks[0].id, force=False, dry_run=False, continue_run=True)


def test_continue_skips_success_and_publishes_same_batch(interrupted_batch):
    """成功原文件不变，未完成分类重跑后 raw/Manifest/商品/CSV 完整一致。"""
    # 保存成功分类文件指纹和修改时间，避免只检验数据库状态。
    config, database, prepared, _, calls, root, _ = interrupted_batch
    files = list((prepared.storage.categories_dir / prepared.category_run_plans[0].category_run_id).glob("*.gz"))
    before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in files}
    assert _run(config) == 0
    assert calls == [prepared.category_run_plans[1].category.key]
    assert before == {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in files}
    # 发布仍属于原批次，成功数据和补录数据合计42条。
    snapshot = database.collection_snapshot(prepared.batch_id)
    assert snapshot.status == "success" and snapshot.published_at is not None
    assert snapshot.collected_item_count == 42 and snapshot.saved_page_count == (4 if config.execution_platform() == "taobao" else 6)
    assert json.loads(prepared.storage.manifest_path.read_text())["status"] == "success"
    with Path(snapshot.csv_path).open(encoding="utf-8-sig") as stream:
        assert len(list(csv.DictReader(stream))) == 42
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CollectionBatch)) == 1
        assert session.scalar(select(func.count()).select_from(ProductRankEntryModel)) == 42
        if config.execution_platform() == "compass":
            # 同时检查跳过分类和重采分类的金额/销量，防止抖音单位被错误恢复。
            rows = session.scalars(select(ProductRankEntryModel)).all()
            assert all(row.pay_amount_min_value == 1000 and row.pay_combo_count_min_value == 10 for row in rows)
    assert list((prepared.storage.categories_dir / "attempts").glob("*/continue/page-001.json.gz"))


def test_continue_source_isolated_by_platform(interrupted_batch):
    """即便任务 ID 相同，另一个平台也不能选中此批次。"""
    # 使用同一测试库查询另一平台，验证平台过滤独立于数据库部署方式。
    config, database, prepared, _, _, _, _ = interrupted_batch
    # 两平台业务身份必须共同参与来源筛选。
    other_platform = "compass" if config.execution_platform() == "taobao" else "taobao"
    assert database.continuation_source(config.tasks[0].id, CAPTURED_AT.date(), platform=other_platform)[0] is None
    assert database.continuation_source(config.tasks[0].id, CAPTURED_AT.date(), platform=config.execution_platform())[0].batch_id == prepared.batch_id
    # 持久化配置被写成另一平台时也不能恢复。
    snapshot = database.collection_snapshot(prepared.batch_id)
    with pytest.raises(ValueError, match="identity mismatch"):
        continuation.continuation_task(replace(snapshot, platform=other_platform))


@pytest.mark.parametrize("status", ["failed", "interrupted", "abandoned", "auth_required"])
def test_continue_accepts_any_unpublished_terminal_reason(interrupted_batch, status):
    """失败原因只影响上次诊断，不妨碍重新执行未完成分类。"""
    # 按生产事务将原批次收口为不同原因，再通过相同入口恢复。
    config, database, prepared, _, calls, _, _ = interrupted_batch
    database.terminate_collection_batch(batch_id=prepared.batch_id, status=status,
        error_category="synthetic_failure", finished_at=CAPTURED_AT,
        current_category_run_id=prepared.category_run_plans[1].category_run_id)
    assert _run(config) == 0
    assert calls == [prepared.category_run_plans[1].category.key]


def test_complete_unpublished_batch_only_retries_publication(interrupted_batch, monkeypatch):
    """所有分类已成功但未发布时，禁止再启动浏览器或重新发现分类。"""
    config, database, prepared, fresh, calls, root, logger = interrupted_batch
    # 先走恢复采集但停在发布前，模拟发布失败留下的完整材料。
    snapshot = database.collection_snapshot(prepared.batch_id)
    recovered, completed = continuation.prepare_continuation(snapshot, config.tasks[0], database, root, logger)
    category_collection.collect_category_batch(prepared_batch=recovered, task=config.tasks[0], client=fresh,
        database=database, runtime_logger=logger, completed_runs=completed)
    fresh.close()
    calls.clear()

    def forbid_browser(*args, **kwargs):
        """完整材料重发不能有任何浏览器初始化。"""
        raise AssertionError("browser must not open for publication retry")

    monkeypatch.setattr(fresh, "open_session", forbid_browser)
    assert _run(config) == 0
    assert calls == []


def test_published_today_means_full_collection(interrupted_batch):
    """当天已发布后再次 --continue 等同全量新版本。"""
    config, database, prepared, _, calls, _, _ = interrupted_batch
    assert _run(config) == 0
    calls.clear()
    assert _run(config) == 0
    assert calls == [plan.category.key for plan in prepared.category_run_plans]
    assert database.collection_snapshot(prepared.batch_id).version == 1
    assert database.recent_status(1)[0].version == 2


def test_old_batch_means_full_collection_today(interrupted_batch, monkeypatch):
    """换天后重跑两类，旧批次及其业务日期不变。"""
    config, database, prepared, fresh, calls, _, _ = interrupted_batch
    # 只把新运行的业务时钟推进一天，原材料保持原日期。
    tomorrow = CAPTURED_AT + timedelta(days=1)

    class TomorrowDateTime(datetime):
        """独立新一天的采集时钟。"""
        @classmethod
        def now(cls, tz=None):
            """返回下一业务日。"""
            return tomorrow if tz else tomorrow.replace(tzinfo=None)

    for module in (category_batch, category_collection, continuation, persistence, runner, taobao, adapter_fixtures):
        monkeypatch.setattr(module, "datetime", TomorrowDateTime)
    # 合成控件请求日期也切换为今天，仍由生产匹配器校验。
    monkeypatch.setattr("test_taobao_adapter.CAPTURED_AT", tomorrow)
    assert _run(config) == 0
    assert calls == [plan.category.key for plan in prepared.category_run_plans]
    assert database.collection_snapshot(prepared.batch_id).published_at is None
    assert database.recent_status(1)[0].batch_id != prepared.batch_id


def test_damaged_success_is_recollected(interrupted_batch):
    """状态成功但文件损坏时，不发布残缺数据。"""
    config, _, prepared, _, calls, _, _ = interrupted_batch
    # 破坏的是临时测试文件，不接触用户真实采集材料。
    path = next((prepared.storage.categories_dir / prepared.category_run_plans[0].category_run_id).glob("*.gz"))
    path.write_bytes(b"broken gzip")
    assert _run(config) == 0
    assert calls == [plan.category.key for plan in prepared.category_run_plans]


def test_repeat_continue_keeps_success_and_archives_each_attempt(interrupted_batch):
    """补录准备后再次退出，再次准备仍保留成功文件且归档不会撞名。"""
    config, database, prepared, _, calls, root, logger = interrupted_batch
    continuation.prepare_continuation(database.collection_snapshot(prepared.batch_id), config.tasks[0], database, root, logger)
    assert _run(config) == 0
    assert calls == [prepared.category_run_plans[1].category.key]


@pytest.mark.parametrize("argv", [
    ["run", "--continue", "--platform", "taobao"],
    ["run", "--platform", "taobao", "--force", "--continue"],
    ["run", "--no-gui", "--continue", "--platform", "taobao"],
    ["run", "--continue", "--platform", "compass"],
    ["run", "--platform", "compass", "--force", "--continue"],
    ["run", "--no-gui", "--continue", "--platform", "compass"],
])
def test_continue_is_independent_of_platform_argument(argv):
    """参数顺序及其他运行参数不改变显式平台。"""
    # continue_run 是独立布尔值，不复用平台 dest。
    parsed = build_parser().parse_args(argv)
    assert parsed.continue_run is True
    assert parsed.platform == argv[argv.index("--platform") + 1]


@pytest.mark.parametrize("platform", ["taobao", "compass"])
@pytest.mark.parametrize("gui_mode", [False, True])
def test_cli_passes_continue_without_changing_platform(monkeypatch, gui_mode, platform):
    """GUI 和终端分支都保留平台隔离，同时透传补录开关。"""
    from compass_collector import cli, gui
    # 仅拦截执行边界，不创建窗口或采集任务。
    received = []
    # 入口分别使用各平台配置，验证补录不会被校验器拒绝或切换平台。
    path = "config/taobao.yaml" if platform == "taobao" else "config/tasks.yaml"
    config = load_config(Path(path))
    arguments = ["run", "--continue", "--platform", platform, "--config", path]
    if gui_mode:
        monkeypatch.setattr(gui, "run_gui", lambda selected, request: received.append((selected, request)) or 0)
    else:
        arguments.append("--no-gui")
        monkeypatch.setattr(cli, "run_collection", lambda selected, task, **kwargs: received.append((selected, kwargs)) or 0)
    assert cli._dispatch_configured_command(build_parser().parse_args(arguments), config) == 0
    assert received[0][0].execution_platform() == platform
    assert (received[0][1].continue_run if gui_mode else received[0][1]["continue_run"]) is True


@pytest.mark.parametrize("alias, platform, path", [("tb", "taobao", "config/taobao.yaml"), ("tt", "compass", "config/tasks.yaml")])
def test_continue_make_argument_preserves_platform(alias, platform, path):
    """Make 追加开关不修改平台、配置、任务和原有模式。"""
    from test_make_commands import make_output
    # -n 只检查最终命令，不启动真实采集。
    result = make_output("run", f"PLATFORM={alias}", "CONTINUE=yes", "GUI=no")
    assert result.returncode == 0
    assert "--continue" in result.stdout and f"--platform {platform}" in result.stdout
    assert f'--config "{path}"' in result.stdout
    assert "--no-gui" in result.stdout and "--force" in result.stdout
    assert make_output("run", "PLATFORM=tb", "CONTINUE=typo").returncode != 0


def test_continue_does_not_ignore_active_platform_lock(interrupted_batch):
    """残留 PID 可恢复，但仍在运行的采集不能被补录重置。"""
    from compass_collector.platform_runtime import PlatformRuntime
    from compass_collector.runtime_locks import RuntimeLockBusy
    # 同一进程获取同一平台锁，补录必须在任何状态修改前拒绝。
    config, database, prepared, _, calls, root, _ = interrupted_batch
    with PlatformRuntime(root, config.execution_platform()).operation("collection", config.browser_for(config.execution_platform()).profile_dir):
        with pytest.raises(RuntimeLockBusy):
            _run(config)
    assert calls == []
    assert database.collection_snapshot(prepared.batch_id).categories[1].status == "running"
