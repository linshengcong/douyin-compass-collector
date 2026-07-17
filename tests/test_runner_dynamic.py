"""Dynamic-category runner integration and publication lifecycle tests."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

import compass_collector.runner as runner_module
from compass_collector.config import AppConfig, load_config
from compass_collector.errors import (
    BrowserOperationError,
    HttpRequestError,
    PublicationError,
)
from compass_collector.exporter import StagedCsvExport
from compass_collector.http_client import HttpJsonResponse
from compass_collector.notifier import (
    BatchNotificationSummary,
    TaskNotificationStatus,
)
from compass_collector.persistence import (
    CategoryRun,
    Database,
    ProductRankEntryModel,
)
from compass_collector.runner import (
    build_task_notification_result,
    run_collection,
    run_status,
)
from compass_collector.runtime_logging import RuntimeLogger


# Runner 计划时间和断言统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 真实脱敏分类 Fixture 提供三个动态三级分类。
CATEGORY_FIXTURE_PATH = Path("tests/fixtures/category_tree.json")
# 固定计划时间避免测试依赖当前日期。
PLANNED_AT = datetime(2026, 7, 17, 14, 0, tzinfo=SHANGHAI_TIMEZONE)


def temporary_config(tmp_path: Path) -> AppConfig:
    """Point the checked-in task at an isolated SQLite database."""

    # 真实配置仅替换数据库路径，业务参数保持生产契约。
    config = load_config(Path("config/tasks.yaml"))
    # Pydantic 不可变复制避免污染其他测试。
    database_config = config.database.model_copy(
        update={"path": tmp_path / "runtime" / "data" / "collector.db"}
    )
    return config.model_copy(update={"database": database_config})


def build_rank_payload(category_id: str) -> dict[str, Any]:
    """Build one valid single-item first page for a dynamic category."""

    return {
        "st": 0,
        "data": {
            "data_result": [
                {
                    "product_info": {
                        "id": f"{category_id}-product-1",
                        "name": f"{category_id} 商品",
                        "rank": 1,
                        "newly_on_ranking": False,
                        "shop_list": [
                            {
                                "shop_id": f"{category_id}-shop-1",
                                "shop_name": f"{category_id} 店铺",
                            }
                        ],
                    },
                    "new_pay_amt": {
                        "value_range": [
                            {"value": 100_000, "unit": "price"},
                            {"value": 200_000, "unit": "price"},
                        ]
                    },
                    "pay_combo_cnt": {
                        "value_range": [
                            {"value": 100, "unit": "number"},
                            {"value": 200, "unit": "number"},
                        ]
                    },
                }
            ],
            "page_result": {
                "page_no": 1,
                "page_size": 10,
                "total": 1,
            },
        },
    }


class FakeBrowserSession:
    """Expose the authenticated browser boundary without starting Chrome."""

    def __init__(self, *, has_cookies: bool = True) -> None:
        """Initialize authentication and lifecycle counters."""

        # has_cookies 决定 runner 是否进入 HTTP 动态链路。
        self.has_cookies = has_cookies
        # close_count 验证 Scheduler/终端非手动路径自动关闭浏览器。
        self.close_count = 0
        # wait_count 验证 manual=False 永不等待键盘输入。
        self.wait_count = 0

    def whitelisted_cookies(self, cookie_names: list[str]) -> list[dict[str, str]]:
        """Return one synthetic cookie without exposing real authentication."""

        if not self.has_cookies:
            return []
        return [
            {
                "name": "synthetic_session",
                "value": "test-only",
                "domain": ".jinritemai.com",
                "path": "/",
            }
        ]

    def user_agent(self) -> str:
        """Return one deterministic browser user agent."""

        return "runner-dynamic-test"

    def wait_for_manual_exit(self, message: str) -> None:
        """Record an unexpected manual wait instead of blocking pytest."""

        self.wait_count += 1

    def close(self) -> None:
        """Record automatic browser cleanup."""

        self.close_count += 1


class FakeCompassClient:
    """Serve one category tree and deterministic category ranking pages."""

    def __init__(self, *, failed_category_ids: set[str] | None = None) -> None:
        """Initialize request counters and ordinary failure categories."""

        # 分类树响应每个顶层任务只能请求一次。
        self.category_tree_calls = 0
        # ranking_calls 保留严格的分类与页码请求顺序。
        self.ranking_calls: list[tuple[str, int]] = []
        # failed_category_ids 模拟不重试的普通网络失败。
        self.failed_category_ids = set(failed_category_ids or ())
        # close_count 验证 runner 最终释放同一个 HTTP 客户端。
        self.close_count = 0

    def get_category_tree(self, params: dict[str, str | int]) -> HttpJsonResponse:
        """Return a fresh sanitized category payload."""

        self.category_tree_calls += 1
        # 每次重新解析避免测试间共享可变结构。
        payload = json.loads(CATEGORY_FIXTURE_PATH.read_text(encoding="utf-8"))
        return HttpJsonResponse(
            payload=payload,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            status_code=200,
        )

    def get_product_rank_page(
        self,
        task: Any,
        params: dict[str, str | int],
    ) -> HttpJsonResponse:
        """Return one item or raise the configured ordinary failure."""

        # 动态 category_id 必须是二级、三级分类组成的级联路径。
        category_path = str(params["category_id"])
        # category_path_parts 让 Runner 集成测试拒绝退化成单个叶子 ID。
        category_path_parts = category_path.split(",")
        if len(category_path_parts) != 2 or not all(category_path_parts):
            raise AssertionError("category_id must contain level-two and level-three IDs")
        # 假响应和失败注入继续使用级联路径末段的三级叶子 ID。
        category_id = category_path_parts[-1]
        # page_no 经请求构造保证为正整数。
        page_no = int(params["page_no"])
        self.ranking_calls.append((category_id, page_no))
        if category_id in self.failed_category_ids:
            raise HttpRequestError(
                "Synthetic request failure",
                category="network_error",
            )
        # 每个分类 total=1，因此只允许请求第一页。
        payload = build_rank_payload(category_id)
        return HttpJsonResponse(payload=payload, body=b"sanitized", status_code=200)

    def close(self) -> None:
        """Record HTTP resource cleanup."""

        self.close_count += 1


def install_runner_fakes(
    *,
    monkeypatch: Any,
    tmp_path: Path,
    client: FakeCompassClient,
) -> tuple[FakeBrowserSession, list[BatchNotificationSummary]]:
    """Install one authenticated browser, HTTP client, and notification sink."""

    # 所有 runtime 文件进入 pytest 临时目录。
    monkeypatch.setattr("compass_collector.runner.RUNTIME_ROOT", tmp_path / "runtime")
    # 同一个浏览器对象用于生命周期断言。
    browser = FakeBrowserSession()
    monkeypatch.setattr(
        "compass_collector.runner.open_browser",
        lambda browser_config: browser,
    )
    # Runner 构造 HTTP 客户端时直接复用传入的可观察实例。
    monkeypatch.setattr(
        "compass_collector.runner.CompassHttpClient",
        lambda *args, **kwargs: client,
    )
    # 通知只捕获安全 summary，不访问真实 Webhook。
    notifications: list[BatchNotificationSummary] = []
    monkeypatch.setattr(
        "compass_collector.runner.deliver_batch_notification",
        lambda summary, runtime_logger: notifications.append(summary),
    )
    return browser, notifications


def run_fake_collection(
    *,
    config: AppConfig,
    dry_run: bool,
) -> int:
    """Run the single configured task at one fixed plan time."""

    # 任务 ID 直接来自真实配置，避免测试硬编码第二份路由。
    task = config.tasks[0]
    return run_collection(
        config,
        selected_task_id=task.id,
        force=False,
        dry_run=dry_run,
        manual=False,
        planned_at_overrides={task.id: PLANNED_AT},
    )


def test_official_runner_publishes_dynamic_categories_and_chinese_csv(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """Publish one successful task after one tree request and three category pages."""

    # 真实配置、假客户端和进程边界全部隔离。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    browser, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )

    exit_code = run_fake_collection(config=config, dry_run=False)

    database = Database(config.database.path)
    try:
        # 正式商品表只在发布事务中写入三个成功分类商品。
        status_rows = database.recent_status(limit=5)
        with database.session_factory() as session:
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()
    # 正式 CSV 应包含表头和四个跨一级分类商品数据行。
    csv_path = Path(status_rows[0].csv_path or "")
    csv_lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    # 清空采集日志后单独核对 status 的扩展列。
    capsys.readouterr()
    status_exit_code = run_status(config, limit=5)
    status_output = capsys.readouterr().out
    # runtime_events 验证 runner 把任务日志归入通知使用的同一执行批次。
    log_path = next((tmp_path / "runtime" / "logs").glob("*.jsonl"))
    runtime_events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    # business_events 仅包含可定位到真实 task batch_id 的日志。
    business_events = [event for event in runtime_events if event.get("batch_id")]

    assert exit_code == 0
    assert status_exit_code == 0
    assert client.category_tree_calls == 1
    assert client.ranking_calls == [
        ("fixture-level3-dresses", 1),
        ("fixture-level3-biscuit", 1),
        ("fixture-level3-seafood", 1),
        ("fixture-level3-tea", 1),
    ]
    assert client.close_count == 1
    assert browser.close_count == 1
    assert browser.wait_count == 0
    assert len(status_rows) == 1
    assert status_rows[0].status == "success"
    assert status_rows[0].published_at is not None
    assert status_rows[0].version == 1
    assert product_count == 4
    assert csv_path.parent.name == config.tasks[0].id
    assert csv_path.parent.parent.name == "2026-07-17"
    assert csv_path.name.startswith("全行业三级分类商品实时榜_1400")
    assert len(csv_lines) == 5
    assert notifications[0].tasks[0].status is TaskNotificationStatus.SUCCESS
    assert notifications[0].tasks[0].saved_items == 4
    assert business_events
    assert {event["execution_batch_id"] for event in business_events} == {
        notifications[0].batch_id
    }
    assert "mode" in status_output
    assert "published" in status_output
    assert "normal" in status_output
    assert "yes" in status_output
    assert "4/0/4" in status_output


def test_same_display_name_and_planned_time_use_task_isolated_csv_directories(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Publish same-named tasks without making their CSV targets conflict."""

    # 两个任务只改变稳定 ID，中文展示名和计划时间故意完全相同。
    base_config = temporary_config(tmp_path)
    first_task = base_config.tasks[0].model_copy(
        update={"id": "food_rank_primary"}
    )
    second_task = base_config.tasks[0].model_copy(
        update={"id": "food_rank_secondary"}
    )
    # AppConfig 保留同一个数据库并按配置顺序运行两个启用任务。
    config = base_config.model_copy(update={"tasks": [first_task, second_task]})
    # 同一个假客户端用于验证每个顶层任务分别请求一次分类树。
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )
    # 两个任务共享计划时间，确保最终中文文件名也完全相同。
    planned_at_overrides = {
        first_task.id: PLANNED_AT,
        second_task.id: PLANNED_AT,
    }

    exit_code = run_collection(
        config,
        selected_task_id=None,
        force=False,
        dry_run=False,
        manual=False,
        planned_at_overrides=planned_at_overrides,
    )

    # SQLite 中的完整 csv_path 用于核对两个正式文件的任务目录边界。
    database = Database(config.database.path)
    try:
        status_by_task = {
            row.task_id: row for row in database.recent_status(limit=10)
        }
    finally:
        database.close()
    # 两个正式路径只允许任务目录不同，basename 必须保持中文展示契约。
    first_csv_path = Path(status_by_task[first_task.id].csv_path or "")
    second_csv_path = Path(status_by_task[second_task.id].csv_path or "")
    # 通知继续只携带 basename，不能泄漏本机目录结构。
    notification_by_task = {
        task.task_id: task for task in notifications[0].tasks
    }

    assert exit_code == 0
    assert client.category_tree_calls == 2
    assert client.ranking_calls == [
        ("fixture-level3-dresses", 1),
        ("fixture-level3-biscuit", 1),
        ("fixture-level3-seafood", 1),
        ("fixture-level3-tea", 1),
        ("fixture-level3-dresses", 1),
        ("fixture-level3-biscuit", 1),
        ("fixture-level3-seafood", 1),
        ("fixture-level3-tea", 1),
    ]
    assert first_csv_path.name == second_csv_path.name
    assert first_csv_path.parent.name == first_task.id
    assert second_csv_path.parent.name == second_task.id
    assert first_csv_path.parent.parent.name == "2026-07-17"
    assert second_csv_path.parent.parent.name == "2026-07-17"
    assert first_csv_path.exists()
    assert second_csv_path.exists()
    assert notification_by_task[first_task.id].csv_filename == first_csv_path.name
    assert notification_by_task[second_task.id].csv_filename == second_csv_path.name


def test_partial_success_is_published_warned_and_returns_zero(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Publish three complete categories after one ordinary category failure."""

    # 第一个三级分类失败一次，后续分类必须继续且不得重试失败分类。
    config = temporary_config(tmp_path)
    client = FakeCompassClient(
        failed_category_ids={"fixture-level3-biscuit"},
    )
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )

    exit_code = run_fake_collection(config=config, dry_run=False)

    database = Database(config.database.path)
    try:
        status_rows = database.recent_status(limit=5)
        with database.session_factory() as session:
            # 分类状态保留一个 failed 和三个 success。
            category_statuses = session.scalars(
                select(CategoryRun.status).order_by(CategoryRun.discovery_order)
            ).all()
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()

    assert exit_code == 0
    assert client.ranking_calls == [
        ("fixture-level3-dresses", 1),
        ("fixture-level3-biscuit", 1),
        ("fixture-level3-seafood", 1),
        ("fixture-level3-tea", 1),
    ]
    assert status_rows[0].status == "partial_success"
    assert status_rows[0].failed_category_count == 1
    assert status_rows[0].successful_category_count == 3
    assert status_rows[0].published_at is not None
    assert category_statuses == ["success", "failed", "success", "success"]
    assert product_count == 3
    assert notifications[0].tasks[0].status is TaskNotificationStatus.PARTIAL_SUCCESS
    assert notifications[0].tasks[0].saved_items == 3
    assert notifications[0].tasks[0].csv_filename is not None


def test_dry_run_keeps_sqlite_raw_audit_without_csv_or_product_rows(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Finalize a dry-run success while leaving official publication fields empty."""

    # dry-run 使用与正式运行完全相同的分类发现和分页链路。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )

    exit_code = run_fake_collection(config=config, dry_run=True)

    database = Database(config.database.path)
    try:
        status_rows = database.recent_status(limit=5)
        with database.session_factory() as session:
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()
    # dry-run 不得在 runtime/exports 下创建任何 CSV。
    exported_csv_files = list((tmp_path / "runtime" / "exports").rglob("*.csv"))

    assert exit_code == 0
    assert status_rows[0].mode == "dry_run"
    assert status_rows[0].status == "success"
    assert status_rows[0].version is None
    assert status_rows[0].csv_path is None
    assert status_rows[0].published_at is None
    assert status_rows[0].saved_page_count == 4
    assert status_rows[0].collected_item_count == 4
    assert product_count == 0
    assert exported_csv_files == []
    assert notifications[0].tasks[0].status is TaskNotificationStatus.SUCCESS
    assert notifications[0].tasks[0].saved_items == 4


def test_dry_run_finalize_keyboard_interrupt_closes_running_batch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Persist interrupted when dry-run finalization aborts before its commit."""

    def interrupt_before_finalize_commit(
        database: Database,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Raise before finalize_dry_run can change the running SQLite batch."""

        raise KeyboardInterrupt

    # 采集保持真实，只把 dry-run SQLite 终结入口改为提交前中止。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )
    monkeypatch.setattr(
        Database,
        "finalize_dry_run",
        interrupt_before_finalize_commit,
    )

    exit_code = run_fake_collection(config=config, dry_run=True)

    database = Database(config.database.path)
    try:
        # SQLite 必须从 running 原子进入 interrupted。
        status_row = database.recent_status(limit=1)[0]
    finally:
        database.close()
    # Manifest 同步使用同一个 interrupted 权威快照。
    manifest_path = next((tmp_path / "runtime" / "raw").rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert status_row.status == "interrupted"
    assert status_row.error_category == "interrupted"
    assert status_row.published_at is None
    assert manifest["status"] == "interrupted"
    assert notifications[0].tasks[0].status is TaskNotificationStatus.INTERRUPTED


@pytest.mark.parametrize(
    ("dry_run", "expected_published"),
    [(True, False), (False, True)],
)
def test_committed_result_survives_return_boundary_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: Any,
    dry_run: bool,
    expected_published: bool,
) -> None:
    """Use SQLite success when interruption happens after commit but before return."""

    # 真实数据库方法先提交，再由包装器模拟返回赋值前的 KeyboardInterrupt。
    if dry_run:
        original_finalize = Database.finalize_dry_run

        def finalize_then_interrupt(
            database: Database,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """Commit the dry-run terminal state before interrupting its return."""

            original_finalize(database, *args, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(Database, "finalize_dry_run", finalize_then_interrupt)
    else:
        original_publish = Database.publish_collected_batch

        def publish_then_interrupt(
            database: Database,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """Commit official rows and CSV before interrupting its return."""

            original_publish(database, *args, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(
            Database,
            "publish_collected_batch",
            publish_then_interrupt,
        )

    # 采集与对应发布事务保持真实，只有事务返回边界被中止。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )

    exit_code = run_fake_collection(config=config, dry_run=dry_run)

    database = Database(config.database.path)
    try:
        # SQLite 是提交是否成功的唯一权威来源。
        status_row = database.recent_status(limit=1)[0]
    finally:
        database.close()

    assert exit_code == 1
    assert status_row.status == "success"
    assert (status_row.published_at is not None) is expected_published
    assert (status_row.csv_path is not None) is expected_published
    assert notifications[0].tasks[0].status is TaskNotificationStatus.SUCCESS
    assert (notifications[0].tasks[0].csv_filename is not None) is expected_published


@pytest.mark.parametrize(
    ("dry_run", "expected_published"),
    [(True, False), (False, True)],
)
def test_committed_result_survives_notification_assignment_interrupt(
    tmp_path: Path,
    monkeypatch: Any,
    dry_run: bool,
    expected_published: bool,
) -> None:
    """Recover success when interruption hits committed-result assignment."""

    # 原始构造器在第二次恢复调用时生成 SQLite 权威成功通知。
    original_builder = runner_module._build_committed_task_result
    # build_attempts 证明首次赋值被中断、保护区内随后完成一次恢复。
    build_attempts: list[str] = []

    def interrupt_first_committed_result(**kwargs: Any) -> Any:
        """Interrupt the first committed-result assignment and then recover."""

        build_attempts.append(kwargs["snapshot"].status)
        if len(build_attempts) == 1:
            raise KeyboardInterrupt
        return original_builder(**kwargs)

    # 数据库提交保持真实，只在成功通知首次赋值入口注入中断。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )
    monkeypatch.setattr(
        runner_module,
        "_build_committed_task_result",
        interrupt_first_committed_result,
    )

    exit_code = run_fake_collection(config=config, dry_run=dry_run)

    database = Database(config.database.path)
    try:
        # SQLite 成功终态必须与恢复后的通知和正式 CSV 保持一致。
        status_row = database.recent_status(limit=1)[0]
    finally:
        database.close()
    # official_csv_exists 只在正式发布模式核对已提交文件仍存在。
    official_csv_exists = (
        Path(status_row.csv_path).exists()
        if status_row.csv_path is not None
        else False
    )

    assert exit_code == 1
    assert build_attempts == ["success", "success"]
    assert status_row.status == "success"
    assert (status_row.published_at is not None) is expected_published
    assert official_csv_exists is expected_published
    assert notifications[0].tasks[0].status is TaskNotificationStatus.SUCCESS
    assert (notifications[0].tasks[0].csv_filename is not None) is expected_published


@pytest.mark.parametrize(
    ("dry_run", "expected_published"),
    [(True, False), (False, True)],
)
def test_committed_result_survives_manifest_sync_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: Any,
    dry_run: bool,
    expected_published: bool,
) -> None:
    """Keep the committed success notification when terminal Manifest sync stops."""

    # 原始同步继续处理采集期 running 快照。
    original_sync = runner_module._sync_collection_snapshot
    # interrupted_syncs 证明 KeyboardInterrupt 发生在提交后的终态投影。
    interrupted_syncs: list[str] = []

    def interrupt_terminal_sync(storage: Any, snapshot: Any) -> None:
        """Interrupt only the committed success snapshot projection."""

        if snapshot.status in {"success", "partial_success"}:
            interrupted_syncs.append(snapshot.status)
            raise KeyboardInterrupt
        original_sync(storage, snapshot)

    # 采集和 SQLite 提交保持真实，只中止终态 Manifest 投影。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )
    monkeypatch.setattr(
        runner_module,
        "_sync_collection_snapshot",
        interrupt_terminal_sync,
    )

    exit_code = run_fake_collection(config=config, dry_run=dry_run)

    database = Database(config.database.path)
    try:
        # Manifest 中止不能反向改写已提交的 SQLite 状态。
        status_row = database.recent_status(limit=1)[0]
    finally:
        database.close()

    assert exit_code == 1
    assert interrupted_syncs == ["success"]
    assert status_row.status == "success"
    assert (status_row.published_at is not None) is expected_published
    assert (status_row.csv_path is not None) is expected_published
    assert notifications[0].tasks[0].status is TaskNotificationStatus.SUCCESS


@pytest.mark.parametrize(
    ("dry_run", "terminal_event", "expected_published"),
    [
        (False, "publication_succeeded", True),
        (True, "dry_run_succeeded", False),
    ],
)
def test_persisted_success_survives_terminal_log_failure(
    tmp_path: Path,
    monkeypatch: Any,
    dry_run: bool,
    terminal_event: str,
    expected_published: bool,
) -> None:
    """Keep a committed success when its final JSONL event cannot be written."""

    # 真实采集和发布链路只把指定终态日志替换为可控 OSError。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )
    # 原始 emit 继续处理分类、分页和其他生命周期事件。
    original_emit = RuntimeLogger.emit
    # failed_events 证明测试确实命中了目标终态日志边界。
    failed_events: list[str] = []

    def fail_terminal_emit(
        runtime_logger: RuntimeLogger,
        **event_fields: Any,
    ) -> None:
        """Raise only for the selected post-commit success event."""

        # event_name 来自 runner 内部稳定事件常量。
        event_name = str(event_fields.get("event") or "")
        if event_name == terminal_event:
            failed_events.append(event_name)
            raise OSError("synthetic terminal log failure")
        original_emit(runtime_logger, **event_fields)

    monkeypatch.setattr(RuntimeLogger, "emit", fail_terminal_emit)

    exit_code = run_fake_collection(config=config, dry_run=dry_run)

    database = Database(config.database.path)
    try:
        # 最近批次是 SQLite 权威终态，不能被非权威日志异常回滚或改写。
        status_row = database.recent_status(limit=1)[0]
    finally:
        database.close()

    assert exit_code == 0
    assert failed_events == [terminal_event]
    assert status_row.status == "success"
    assert (status_row.published_at is not None) is expected_published
    assert (status_row.csv_path is not None) is expected_published
    assert notifications[0].tasks[0].status is TaskNotificationStatus.SUCCESS


def test_publication_failure_closes_running_batch_without_csv(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Convert a CSV preparation failure into one unpublished failed batch."""

    # 采集本身成功，发布入口被替换为稳定 PublicationError。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )

    def fail_csv_prepare(self: Any, **kwargs: Any) -> Any:
        """Raise before any temporary or final CSV is created."""

        raise PublicationError(
            "Synthetic CSV failure",
            category="csv_write_error",
        )

    monkeypatch.setattr(
        "compass_collector.runner.CsvExporter.prepare",
        fail_csv_prepare,
    )

    exit_code = run_fake_collection(config=config, dry_run=False)

    database = Database(config.database.path)
    try:
        status_rows = database.recent_status(limit=5)
        with database.session_factory() as session:
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()
    # 发布失败必须清理临时状态并保持 published_at 唯一判据为空。
    exported_csv_files = list((tmp_path / "runtime" / "exports").rglob("*.csv"))

    assert exit_code == 1
    assert status_rows[0].status == "failed"
    assert status_rows[0].error_category == "csv_write_error"
    assert status_rows[0].version is None
    assert status_rows[0].csv_path is None
    assert status_rows[0].published_at is None
    assert product_count == 0
    assert exported_csv_files == []
    assert notifications[0].tasks[0].status is TaskNotificationStatus.FAILED
    assert notifications[0].tasks[0].saved_items == 4


def test_publication_keyboard_interrupt_closes_batch_and_removes_csv(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Rollback the staged CSV and persist interrupted instead of leaving running."""

    # 真实发布方法先完成原子移动，再由测试中止事务。
    original_publish = StagedCsvExport.publish

    def publish_then_interrupt(staged_csv: StagedCsvExport) -> None:
        """Interrupt after the final CSV exists to exercise full compensation."""

        original_publish(staged_csv)
        raise KeyboardInterrupt

    # 采集链路保持真实，仅替换发布动作的中止时点。
    config = temporary_config(tmp_path)
    client = FakeCompassClient()
    _, notifications = install_runner_fakes(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        client=client,
    )
    monkeypatch.setattr(StagedCsvExport, "publish", publish_then_interrupt)

    exit_code = run_fake_collection(config=config, dry_run=False)

    database = Database(config.database.path)
    try:
        status_rows = database.recent_status(limit=5)
        with database.session_factory() as session:
            # 正式商品行必须跟随发布事务一起回滚。
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()
    # 中止不得留下正式或临时 CSV 文件。
    export_files = [
        path
        for path in (tmp_path / "runtime" / "exports").rglob("*")
        if path.is_file()
    ]

    assert exit_code == 1
    assert len(status_rows) == 1
    assert status_rows[0].status == "interrupted"
    assert status_rows[0].error_category == "interrupted"
    assert status_rows[0].version is None
    assert status_rows[0].csv_path is None
    assert status_rows[0].published_at is None
    assert product_count == 0
    assert export_files == []
    assert notifications[0].tasks[0].status is TaskNotificationStatus.INTERRUPTED
    assert notifications[0].tasks[0].saved_items == 4


def test_failed_notification_excludes_partial_raw_items_without_successful_runs(
    tmp_path: Path,
) -> None:
    """Never report failed-category raw rows as successfully collected items."""

    # Manifest 模拟失败分类已经保存五条 raw，但没有任何完整成功分类。
    storage = SimpleNamespace(
        manifest={"saved_page_count": 1, "collected_item_count": 5}
    )
    # 真实任务只提供通知展示名和稳定 task_id。
    task = temporary_config(tmp_path).tasks[0]

    result = build_task_notification_result(
        task,
        TaskNotificationStatus.FAILED,
        storage=storage,  # type: ignore[arg-type]
        category_runs=(),
        error_category="network_error",
    )

    assert result.saved_pages == 0
    assert result.saved_items == 0


def test_browser_failure_persists_safe_page_diagnostics_in_new_batch_storage(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Keep the screenshot contract after removing the legacy RunStorage path."""

    # 临时配置确保浏览器失败批次和诊断材料都落在 pytest 目录。
    config = temporary_config(tmp_path)
    # 所有 runtime 文件进入隔离目录，不能触碰真实 Chrome Profile。
    monkeypatch.setattr("compass_collector.runner.RUNTIME_ROOT", tmp_path / "runtime")
    # 固定 PNG 字节用于验证 runner 确实把 BrowserOperationError 交给 BatchStorage。
    screenshot = b"\x89PNG\r\n\x1a\nrunner-browser-fixture"

    def fail_browser_start(browser_config: Any) -> Any:
        """Raise one safe page failure without starting a real browser."""

        raise BrowserOperationError(
            "Synthetic browser failure",
            category="browser_page_error",
            failed_step="open_safe_page",
            exception_type="TimeoutError",
            safe_page_path="/shop/chance/rank-product",
            page_title="电商罗盘",
            screenshot=screenshot,
        )

    monkeypatch.setattr("compass_collector.runner.open_browser", fail_browser_start)
    # 通知只收集内存摘要，禁止测试访问真实 Webhook。
    notifications: list[BatchNotificationSummary] = []
    monkeypatch.setattr(
        "compass_collector.runner.deliver_batch_notification",
        lambda summary, runtime_logger: notifications.append(summary),
    )

    exit_code = run_fake_collection(config=config, dry_run=False)

    database = Database(config.database.path)
    try:
        # 唯一失败批次提供实际 batch_id，用于定位新目录结构。
        status_row = database.recent_status(limit=1)[0]
    finally:
        database.close()
    # 新 BatchStorage 的批次级 artifact 目录不再包含旧 run_id/task_id 嵌套。
    artifact_directory = (
        tmp_path
        / "runtime"
        / "artifacts"
        / PLANNED_AT.date().isoformat()
        / config.tasks[0].id
        / status_row.batch_id
    )
    # 安全 JSON 用于核对页面元数据，不读取或输出异常原文。
    failure_summary = json.loads(
        (artifact_directory / "failure.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert status_row.status == "failed"
    assert status_row.error_category == "browser_page_error"
    assert (artifact_directory / "failure.png").read_bytes() == screenshot
    assert failure_summary["safe_page_path"] == "/shop/chance/rank-product"
    assert failure_summary["page_title"] == "电商罗盘"
    assert failure_summary["screenshot_saved"] is True
    assert notifications[0].tasks[0].status is TaskNotificationStatus.FAILED
