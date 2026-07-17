"""Stage-two category-tree orchestration tests without ranking requests."""

import gzip
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from compass_collector.category_batch import prepare_category_batch
from compass_collector.config import load_config
from compass_collector.errors import (
    AuthRequiredError,
    CategoryBatchPreparationError,
)
from compass_collector.http_client import HttpJsonResponse
from compass_collector.persistence import (
    CategoryRun,
    CollectionBatch,
    Database,
    upgrade_database,
)
from compass_collector.raw_storage import BatchStorage
from compass_collector.run_control import CollectionControl
from compass_collector.runtime_logging import RuntimeLogger


# 精简脱敏分类树 Fixture 复用解析器测试的真实响应形状。
FIXTURE_PATH = Path("tests/fixtures/category_tree.json")
# 计划时间统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 固定业务日期用于核对 runtime 目录结构。
BUSINESS_DATE = date(2026, 7, 17)
# 固定计划时间用于 Manifest 和 SQLite 断言。
PLANNED_AT = datetime(2026, 7, 17, 14, 0, tzinfo=SHANGHAI_TIMEZONE)


def load_category_payload() -> dict[str, Any]:
    """Load a fresh sanitized category response for each orchestration test."""

    # 重新解析 JSON，避免失败测试修改共享对象。
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def create_database(tmp_path: Path) -> Database:
    """Create one migrated test database under the isolated runtime root."""

    # 数据库文件不触碰仓库真实 runtime。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    return Database(database_path)


class SuccessfulCategoryClient:
    """Return one prepared category response and reject ranking calls."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store one response and initialize the explicit request counter."""

        # payload 是本测试唯一允许返回的分类响应。
        self.payload = payload
        # request_count 证明每个顶层任务只请求一次分类树。
        self.request_count = 0

    def get_category_tree(self, params: dict[str, str | int]) -> HttpJsonResponse:
        """Return the configured payload for one category request."""

        self.request_count += 1
        return HttpJsonResponse(
            payload=self.payload,
            body=json.dumps(self.payload, ensure_ascii=False).encode("utf-8"),
            status_code=200,
        )

    def get_product_rank_page(self, *args: Any, **kwargs: Any) -> HttpJsonResponse:
        """Fail if stage two attempts any ranking-page request."""

        raise AssertionError("stage two must not request ranking pages")


class AuthFailureCategoryClient:
    """Raise one authentication error at the category-tree boundary."""

    def __init__(self) -> None:
        """Initialize the explicit request counter."""

        # request_count distinguishes one failed request from retries。
        self.request_count = 0

    def get_category_tree(self, params: dict[str, str | int]) -> HttpJsonResponse:
        """Raise auth_required without returning a category response."""

        self.request_count += 1
        raise AuthRequiredError(
            "Compass authentication is required",
            category="auth_required",
            status_code=401,
            response_body=b"unauthorized",
        )


class InterruptedCategoryClient:
    """Raise one process-level interruption at the category-tree boundary."""

    def __init__(self, interruption: BaseException) -> None:
        """Store the exact interruption object and initialize request counting."""

        # interruption 必须原样穿过分类发现收口逻辑。
        self.interruption = interruption
        # request_count 证明中止后不会自动重试分类树。
        self.request_count = 0

    def get_category_tree(self, params: dict[str, str | int]) -> HttpJsonResponse:
        """Raise the configured process interruption on the only request."""

        self.request_count += 1
        raise self.interruption


class StopAfterResponseClient(SuccessfulCategoryClient):
    """Request stop after receiving one complete category response."""

    def __init__(
        self,
        payload: dict[str, Any],
        control: CollectionControl,
    ) -> None:
        """Store the shared control used to simulate an in-flight user stop."""

        super().__init__(payload)
        # control 在响应构造后切换为停止状态。
        self.control = control

    def get_category_tree(self, params: dict[str, str | int]) -> HttpJsonResponse:
        """Return one response and then signal that the user clicked stop."""

        # 完整响应先由父实现构造，再模拟请求期间发生的停止动作。
        response = super().get_category_tree(params)
        self.control.request_stop()
        return response


class FailingRuntimeLogger:
    """Simulate an unavailable JSONL destination for lifecycle tests."""

    def __init__(self, exception_type: type[Exception] = OSError) -> None:
        """Store the diagnostic-channel failure type raised by emit."""

        # OSError 模拟磁盘失败，ValueError 模拟安全校验或 sink 失败。
        self.exception_type = exception_type

    def emit(self, **kwargs: Any) -> None:
        """Raise the configured error for every attempted event."""

        raise self.exception_type("simulated log failure")


def test_prepare_category_batch_persists_and_prints_all_categories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Create one running batch with ordered pending categories and no page calls."""

    # 真实任务配置提供全一级分类范围和稳定任务 ID。
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    # HTTP 假客户端只允许一次分类树请求。
    client = SuccessfulCategoryClient(load_category_payload())
    # SQLite、Manifest、raw 和日志全部隔离到 pytest 目录。
    database = create_database(tmp_path)
    logger = RuntimeLogger(tmp_path / "runtime" / "logs")
    try:
        prepared_batch = prepare_category_batch(
            runtime_root=tmp_path / "runtime",
            batch_id="batch-category-success",
            task=task,
            business_date=BUSINESS_DATE,
            planned_at=PLANNED_AT,
            mode="normal",
            client=client,  # type: ignore[arg-type]
            database=database,
            runtime_logger=logger,
        )

        with database.session_factory() as session:
            # 批次在阶段三开始前仍保持 running。
            batch = session.get(CollectionBatch, "batch-category-success")
            # 分类运行必须按 discovery_order 读取。
            category_runs = session.scalars(
                select(CategoryRun)
                .where(CategoryRun.batch_id == "batch-category-success")
                .order_by(CategoryRun.discovery_order)
            ).all()
    finally:
        database.close()

    # raw gzip 必须保留完整分类响应。
    with gzip.open(
        prepared_batch.storage.category_tree_path,
        "rt",
        encoding="utf-8",
    ) as file_handle:
        restored_payload = json.load(file_handle)
    # 唯一 Manifest 用于核对阶段二停点。
    manifest = json.loads(
        prepared_batch.storage.manifest_path.read_text(encoding="utf-8")
    )
    # JSONL 事件用于确认所有分类均打印且带分类运行 ID。
    log_path = next((tmp_path / "runtime" / "logs").glob("*.jsonl"))
    log_events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    # 控制台文本是开发阶段人工检查的直接入口。
    console_output = capsys.readouterr().out

    assert client.request_count == 1
    assert restored_payload == client.payload
    assert prepared_batch.discovery.root_category_name is None
    assert [category.category_name for category in prepared_batch.discovery.categories] == [
        "连衣裙",
        "饼干",
        "海味零食",
        "茶叶",
    ]
    assert batch is not None
    assert batch.status == "running"
    assert batch.discovered_category_count == 4
    assert batch.root_category_id is None
    assert batch.root_category_name is None
    assert [category_run.status for category_run in category_runs] == [
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    assert manifest["status"] == "running"
    assert manifest["discovered_category_count"] == 4
    assert len(manifest["categories"]) == 4
    assert "001 服饰内衣 > 女装 > 连衣裙" in console_output
    assert "004 食品饮料 > 水饮冲调 > 茶叶" in console_output
    assert [
        event["discovery_order"]
        for event in log_events
        if event["event"] == "category_discovered"
    ] == [1, 2, 3, 4]
    assert all(
        event["category_run_id"]
        for event in log_events
        if event["event"] == "category_discovered"
    )
    assert not list(prepared_batch.storage.batch_dir.rglob("page-*.json.gz"))


def test_contract_failure_keeps_raw_tree_and_finishes_batch(tmp_path: Path) -> None:
    """Save the response before rejecting a malformed level-one node."""

    # 任意被遍历的一级节点缺少 ID 时，整棵分类树都不可信。
    payload = load_category_payload()
    payload["data"]["cate_list"][0].pop("cate_id")
    # 分类请求仍成功，失败发生在本地契约解析。
    client = SuccessfulCategoryClient(payload)
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-contract-failure",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
            )
        with database.session_factory() as session:
            # 失败批次仍保留分类树 raw 路径。
            batch = session.get(CollectionBatch, "batch-category-contract-failure")
            # 契约失败时不得创建任何 category_run。
            category_count = session.scalar(
                select(func.count()).select_from(CategoryRun)
            )
    finally:
        database.close()

    assert error_info.value.cause.category == "invalid_category_tree"
    assert batch is not None
    assert batch.status == "failed"
    assert batch.error_category == "invalid_category_tree"
    assert batch.category_tree_raw_path is not None
    assert Path(batch.category_tree_raw_path).exists()
    assert category_count == 0
    assert error_info.value.storage.manifest["status"] == "failed"


def test_auth_failure_has_no_category_tree_and_no_retry(tmp_path: Path) -> None:
    """Finish auth_required before raw or pending category rows are created."""

    # 鉴权失败客户端只允许一次显式请求。
    client = AuthFailureCategoryClient()
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-auth-failure",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
            )
        with database.session_factory() as session:
            # auth_required 是独立批次终态。
            batch = session.get(CollectionBatch, "batch-category-auth-failure")
            # 没有成功响应时分类表保持为空。
            category_count = session.scalar(
                select(func.count()).select_from(CategoryRun)
            )
    finally:
        database.close()

    assert client.request_count == 1
    assert error_info.value.cause.category == "auth_required"
    assert batch is not None
    assert batch.status == "auth_required"
    assert batch.category_tree_raw_path is None
    assert category_count == 0
    assert not error_info.value.storage.category_tree_path.exists()
    assert (
        error_info.value.storage.artifact_dir / "failure-response.txt"
    ).read_bytes() == b"unauthorized"
    # 安全索引不得复制鉴权失败正文。
    failure_index = json.loads(
        (error_info.value.storage.artifact_dir / "failure.json").read_text(
            encoding="utf-8"
        )
    )
    assert failure_index["error_category"] == "auth_required"
    assert "unauthorized" not in json.dumps(failure_index)


@pytest.mark.parametrize(
    ("interruption", "expected_type"),
    (
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(23), SystemExit),
    ),
    ids=("keyboard-interrupt", "system-exit"),
)
def test_category_tree_process_interruption_closes_running_batch(
    tmp_path: Path,
    interruption: BaseException,
    expected_type: type[BaseException],
) -> None:
    """Close SQLite and Manifest before preserving a process interruption."""

    # 中止客户端在 SQLite running 批次创建后、分类树返回前终止流程。
    client = InterruptedCategoryClient(interruption)
    # 真实任务配置保留食品饮料根和全部生产参数契约。
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    # 隔离数据库用于验证进程中止后的持久化终态。
    database = create_database(tmp_path)
    try:
        with pytest.raises(expected_type) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-process-interruption",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
            )
        with database.session_factory() as session:
            # 新 Session 必须看到不再 running 的 interrupted 权威终态。
            batch = session.get(
                CollectionBatch,
                "batch-category-process-interruption",
            )
            # 分类树未返回时不允许创建任何三级分类运行记录。
            category_count = session.scalar(
                select(func.count()).select_from(CategoryRun)
            )
            # Manifest 路径在关闭 Session 前转为独立 Path。
            manifest_path = Path(batch.manifest_path or "") if batch else Path()
    finally:
        database.close()
    # Manifest 是 SQLite 终态的本地审计镜像。
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert error_info.value is interruption
    assert client.request_count == 1
    assert batch is not None
    assert batch.status == "interrupted"
    assert batch.error_category == "interrupted"
    assert batch.finished_at is not None
    assert batch.category_tree_raw_path is None
    assert category_count == 0
    assert manifest["status"] == "interrupted"
    assert manifest["error_category"] == "interrupted"


def test_empty_discovery_keeps_multi_root_scope_without_synthetic_root(tmp_path: Path) -> None:
    """Keep batch root fields null when all level-one nodes yield zero targets."""

    # 所有一级节点都不再提供有效三级分类。
    payload = load_category_payload()
    for level1_node in payload["data"]["cate_list"]:
        level1_node["children"] = []
    # 成功响应必须先保存，再以 category_discovery_empty 结束批次。
    client = SuccessfulCategoryClient(payload)
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-empty",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
            )
        with database.session_factory() as session:
            # SQLite 不得制造不存在的“全部行业”根分类。
            batch = session.get(CollectionBatch, "batch-category-empty")
    finally:
        database.close()

    assert error_info.value.cause.category == "category_discovery_empty"
    assert batch is not None
    assert batch.status == "failed"
    assert batch.root_category_id is None
    assert batch.root_category_name is None
    assert error_info.value.storage.manifest["root_category_id"] is None
    assert error_info.value.storage.manifest["root_category_name"] is None
    assert error_info.value.storage.manifest["categories"] == []


def test_pre_requested_stop_finishes_before_category_http(tmp_path: Path) -> None:
    """Honor developer cancellation before the first category-tree request."""

    # 已请求停止的控制器不得触达 HTTP 客户端。
    control = CollectionControl()
    control.request_stop()
    client = SuccessfulCategoryClient(load_category_payload())
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-interrupted",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
                control=control,
            )
        with database.session_factory() as session:
            # 人工停止保留独立 interrupted 终态。
            batch = session.get(CollectionBatch, "batch-category-interrupted")
    finally:
        database.close()

    assert client.request_count == 0
    assert error_info.value.cause.category == "interrupted"
    assert batch is not None
    assert batch.status == "interrupted"
    assert batch.category_tree_raw_path is None


def test_stop_after_http_preserves_the_received_category_tree(tmp_path: Path) -> None:
    """Persist a completed response before honoring an in-flight stop request."""

    # 控制器由假客户端在返回完整响应后切换为停止。
    control = CollectionControl()
    client = StopAfterResponseClient(load_category_payload(), control)
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-stop-after-response",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
                control=control,
            )
        with database.session_factory() as session:
            # interrupted 批次仍必须索引已经保存的分类树 raw。
            batch = session.get(CollectionBatch, "batch-category-stop-after-response")
            # 停止发生在解析前，不应创建任何 pending 分类。
            category_count = session.scalar(
                select(func.count()).select_from(CategoryRun)
            )
    finally:
        database.close()

    assert error_info.value.cause.category == "interrupted"
    assert batch is not None
    assert batch.status == "interrupted"
    assert batch.category_tree_raw_path is not None
    assert Path(batch.category_tree_raw_path).is_file()
    assert error_info.value.storage.manifest["category_tree_raw_path"] == (
        batch.category_tree_raw_path
    )
    assert category_count == 0


@pytest.mark.parametrize("logger_error_type", [OSError, ValueError])
def test_initial_log_failure_is_wrapped_and_does_not_leave_running_batch(
    tmp_path: Path,
    logger_error_type: type[Exception],
) -> None:
    """Close the batch even when logging fails before the first HTTP request."""

    # 成功客户端用于证明日志失败发生在网络边界之前。
    client = SuccessfulCategoryClient(load_category_payload())
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-log-failure",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=FailingRuntimeLogger(  # type: ignore[arg-type]
                    logger_error_type
                ),
            )
        with database.session_factory() as session:
            # SQLite 必须从 running 收口为稳定失败终态。
            batch = session.get(CollectionBatch, "batch-category-log-failure")
    finally:
        database.close()

    assert client.request_count == 0
    assert error_info.value.cause.category == "internal_error"
    assert batch is not None
    assert batch.status == "failed"
    assert batch.error_category == "internal_error"
    assert error_info.value.storage.manifest["status"] == "failed"


def test_terminal_manifest_write_retries_without_masking_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry one transient Manifest failure and preserve auth_required as the cause."""

    # 首次终态写入故意失败，第二次调用生产实现。
    original_mark_terminal = BatchStorage.mark_batch_terminal
    terminal_attempts = 0

    def flaky_mark_terminal(storage: BatchStorage, **kwargs: Any) -> None:
        """Fail the first terminal write and delegate the second attempt."""

        nonlocal terminal_attempts
        terminal_attempts += 1
        if terminal_attempts == 1:
            raise OSError("simulated manifest failure")
        original_mark_terminal(storage, **kwargs)

    monkeypatch.setattr(BatchStorage, "mark_batch_terminal", flaky_mark_terminal)
    # auth_required 提供需要保留的原始稳定错误分类。
    client = AuthFailureCategoryClient()
    task = load_config(Path("config/tasks.yaml")).tasks[0]
    database = create_database(tmp_path)
    try:
        with pytest.raises(CategoryBatchPreparationError) as error_info:
            prepare_category_batch(
                runtime_root=tmp_path / "runtime",
                batch_id="batch-category-manifest-retry",
                task=task,
                business_date=BUSINESS_DATE,
                planned_at=PLANNED_AT,
                mode="normal",
                client=client,  # type: ignore[arg-type]
                database=database,
                runtime_logger=RuntimeLogger(tmp_path / "runtime" / "logs"),
            )
        with database.session_factory() as session:
            # 数据库和最终 Manifest 必须保持相同终态。
            batch = session.get(CollectionBatch, "batch-category-manifest-retry")
    finally:
        database.close()

    assert terminal_attempts == 2
    assert error_info.value.cause.category == "auth_required"
    assert batch is not None
    assert batch.status == "auth_required"
    assert error_info.value.storage.manifest["status"] == "auth_required"
