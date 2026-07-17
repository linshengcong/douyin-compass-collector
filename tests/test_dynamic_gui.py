"""Dynamic category GUI progress and published CSV selection tests."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from compass_collector import gui as gui_module
from compass_collector.gui import (
    GuiProgressState,
    _business_batch_id,
    latest_published_csv,
    reduce_gui_progress,
)


def test_dynamic_category_events_drive_category_and_page_progress() -> None:
    """Track one discovered category through pagination and batch readiness."""

    # state 从完全空闲的纯状态开始，不构造真实 Qt 窗口。
    state = GuiProgressState()
    state = reduce_gui_progress(state, {"event": "category_batch_started"})
    assert state.stage_text == "阶段：请求分类树"
    assert state.indeterminate is True

    state = reduce_gui_progress(
        state,
        {
            "event": "category_discovery_succeeded",
            "discovered_category_count": 73,
        },
    )
    assert state.category_total == 73
    assert state.progress_text == "已发现 73 个三级分类"

    # category_path 模拟分类树第 12 个动态三级分类。
    category_path = "食品饮料 > 休闲食品 > 坚果炒货"
    state = reduce_gui_progress(
        state,
        {
            "event": "category_collection_started",
            "discovery_order": 12,
            "category_path": category_path,
            "category_run_id": "category-12",
        },
    )
    assert state.category_index == 12
    assert state.category_path == category_path
    assert state.progress_text == "分类 12 / 73 · 等待第 1 页"

    state = reduce_gui_progress(
        state,
        {
            "event": "category_page_saved",
            "page_no": 2,
            "target_pages": 8,
            "saved_items": 20,
            "category_run_id": "category-12",
        },
    )
    assert state.page_no == 2
    assert state.target_pages == 8
    assert state.progress_text == "分类 12 / 73 · 第 2 / 8 页"

    state = reduce_gui_progress(
        state,
        {
            "event": "category_collection_succeeded",
            "saved_items": 73,
            "target_pages": 8,
        },
    )
    assert state.page_no == 8
    assert state.stage_text == f"阶段：分类完成 · {category_path}"

    state = reduce_gui_progress(
        state,
        {
            "event": "category_batch_collection_ready",
            "discovered_category_count": 73,
            "saved_items": 5329,
        },
    )
    assert state.category_index == 73
    assert state.category_total == 73
    assert state.progress_text == "分类 73 / 73 · 采集完成，等待发布"
    assert state.result_text == "分类采集完成，等待发布"


def test_gui_batch_display_ignores_notification_execution_identity() -> None:
    """Keep the visible batch ID tied to a real task batch during notification."""

    # business_event 带 task_id，可定位到真实 SQLite/raw 任务批次。
    business_event = {"batch_id": "task-batch", "task_id": "food-task"}
    # legacy_notification 模拟升级前通知复用 batch_id 的安全事件。
    legacy_notification = {
        "batch_id": "execution-batch",
        "task_id": None,
        "stage": "notification",
    }
    # current_notification 使用独立 execution_batch_id，不再提供业务 batch_id。
    current_notification = {
        "batch_id": None,
        "execution_batch_id": "execution-batch",
        "task_id": None,
        "stage": "notification",
    }

    assert _business_batch_id(business_event) == "task-batch"
    assert _business_batch_id(legacy_notification) is None
    assert _business_batch_id(current_notification) is None


def test_category_failure_preserves_current_category_progress() -> None:
    """Keep the failed category location visible while the batch continues."""

    # state 模拟第 12 个分类已经成功保存第 2/8 页。
    state = GuiProgressState(
        stage_text="阶段：食品饮料 > 休闲食品 > 坚果炒货",
        progress_text="分类 12 / 73 · 第 2 / 8 页",
        category_index=12,
        category_total=73,
        page_no=2,
        target_pages=8,
        category_path="食品饮料 > 休闲食品 > 坚果炒货",
        result_text="采集中",
    )
    # failed_state 只推进失败页，不丢失分类序号、总数和路径。
    failed_state = reduce_gui_progress(
        state,
        {
            "event": "category_collection_failed",
            "page_no": 3,
            "error_category": "http_error",
        },
    )

    assert failed_state.category_index == 12
    assert failed_state.category_total == 73
    assert failed_state.category_path == state.category_path
    assert failed_state.page_no == 3
    assert failed_state.progress_text == "分类 12 / 73 · 第 3 / 8 页 · 失败"
    assert failed_state.result_text == "当前分类采集失败，继续执行后续分类"


def test_terminal_category_failure_overrides_the_continue_message() -> None:
    """Stop claiming the loop continues after the third ordinary failure."""

    # continued_state 模拟普通分类失败后继续执行的临时文案。
    continued_state = GuiProgressState(
        category_index=30,
        category_total=73,
        result_text="当前分类采集失败，继续执行后续分类",
    )
    # terminated_state 必须由随后到达的批次终止事件覆盖。
    terminated_state = reduce_gui_progress(
        continued_state,
        {
            "event": "category_batch_collection_terminated",
            "batch_status": "failed",
            "error_category": "http_error",
        },
    )

    assert terminated_state.stage_text == "阶段：分类采集已终止"
    assert terminated_state.result_text == "分类失败达到终止条件，本次未发布正式数据"


def test_publication_failure_replaces_the_waiting_for_publish_state() -> None:
    """Show an explicit failed terminal result when CSV or SQLite publication fails."""

    # ready_state 是全部分类完成后、正式发布前的 GUI 状态。
    ready_state = GuiProgressState(
        category_index=73,
        category_total=73,
        result_text="分类采集完成，等待发布",
    )
    # failed_state 不能继续显示等待状态，因为 runner 已将批次收口为 failed。
    failed_state = reduce_gui_progress(
        ready_state,
        {
            "event": "publication_failed",
            "error_category": "csv_write_error",
        },
    )

    assert failed_state.stage_text == "阶段：发布失败"
    assert failed_state.progress_text == "分类采集已完成，但正式结果未发布"
    assert failed_state.result_text == "发布失败，未生成正式商品数据或 CSV"


def test_discovery_and_auth_terminal_events_have_explicit_results() -> None:
    """Represent failures that occur before or during the dynamic category loop."""

    # discovery_failed 模拟根分类找不到或分类接口契约变化。
    discovery_failed = reduce_gui_progress(
        GuiProgressState(indeterminate=True),
        {
            "event": "category_discovery_failed",
            "message": "分类发现失败，category=category_root_not_found",
        },
    )
    # auth_failed 模拟分页过程中登录态失效后的 runner 终态事件。
    auth_failed = reduce_gui_progress(
        discovery_failed,
        {"event": "authentication_expired"},
    )

    assert discovery_failed.stage_text == "阶段：分类发现失败"
    assert discovery_failed.result_text == "分类发现失败，category=category_root_not_found"
    assert auth_failed.stage_text == "阶段：登录态失效"
    assert auth_failed.result_text == "登录态失效，本次未发布正式数据"


def test_gui_worker_start_failure_does_not_leave_a_running_message() -> None:
    """Use the safe local worker event when collection never enters the API chain."""

    # failed_state 模拟配置重载或锁检查失败后 QThread 发出的本地安全事件。
    failed_state = reduce_gui_progress(
        GuiProgressState(result_text="正在准备采集", indeterminate=True),
        {
            "event": "gui_worker_failed",
            "message": "采集工作线程启动失败：ValueError",
        },
    )

    assert failed_state.stage_text == "阶段：采集失败"
    assert failed_state.result_text == "采集工作线程启动失败：ValueError"
    assert failed_state.indeterminate is False


def test_partial_success_publication_exposes_csv_and_explicit_result() -> None:
    """Describe an allowed partial publication without hiding failed categories."""

    # csv_path 是正式发布事件提供的本次结果路径。
    csv_path = Path("runtime/exports/食品饮料三级分类商品实时榜.csv")
    # published_state 模拟所有动态分类已经完成处理。
    published_state = reduce_gui_progress(
        GuiProgressState(category_index=73, category_total=73),
        {
            "event": "publication_succeeded",
            "batch_status": "partial_success",
            "version": 2,
            "csv_path": str(csv_path),
        },
    )

    assert published_state.result_text == "部分分类失败，成功结果已发布"
    assert published_state.csv_path == csv_path
    assert published_state.progress_text == "分类 73 / 73 · 发布完成"


def test_success_publication_uses_normal_success_result() -> None:
    """Keep the full-success publication message distinct from partial success."""

    # published_state 不需要真实文件即可验证纯事件语义。
    published_state = reduce_gui_progress(
        GuiProgressState(category_index=73, category_total=73),
        {
            "event": "publication_succeeded",
            "batch_status": "success",
            "csv_path": "runtime/exports/result.csv",
        },
    )

    assert published_state.result_text == "正式采集成功"


def test_partial_success_dry_run_reports_failures_without_publication() -> None:
    """Expose partial dry-run failures while keeping publication status explicit."""

    # dry_run_state 模拟少量分类失败但其余分类校验完成的试运行。
    dry_run_state = reduce_gui_progress(
        GuiProgressState(category_index=73, category_total=73),
        {
            "event": "dry_run_succeeded",
            "batch_status": "partial_success",
        },
    )

    assert dry_run_state.result_text == "部分分类失败，试运行已完成（未发布正式数据）"
    assert dry_run_state.csv_path is None


def test_latest_published_csv_uses_published_at_and_skips_missing_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Include official partial success while excluding unpublished dry-runs."""

    # missing_csv 模拟已经被人工移动的最新正式文件。
    missing_csv = tmp_path / "missing.csv"
    # dry_run_csv 存在但 published_at 为空，不能成为正式打开目标。
    dry_run_csv = tmp_path / "dry-run.csv"
    dry_run_csv.write_text("dry-run", encoding="utf-8")
    # partial_csv 是本测试应返回的最近仍存在正式发布文件。
    partial_csv = tmp_path / "partial.csv"
    partial_csv.write_text("partial", encoding="utf-8")
    # status_rows 保持 recent_status 的从新到旧顺序。
    status_rows = [
        SimpleNamespace(
            status="success",
            published_at=datetime(2026, 7, 17, 12, 0),
            csv_path=str(missing_csv),
        ),
        SimpleNamespace(
            status="success",
            published_at=None,
            csv_path=str(dry_run_csv),
        ),
        SimpleNamespace(
            status="partial_success",
            published_at=datetime(2026, 7, 17, 11, 0),
            csv_path=str(partial_csv),
        ),
    ]
    # database_closed 验证短生命周期查询总会关闭连接。
    database_closed: list[bool] = []

    class FakeDatabase:
        """Provide deterministic status rows without creating a real GUI database."""

        def __init__(self, database_path: Path) -> None:
            """Accept the same constructor boundary as the production database."""

            # database_path 仅用于证明 GUI 传入配置中的路径。
            self.database_path = database_path

        def recent_status(self, limit: int) -> list[SimpleNamespace]:
            """Return the prepared newest-first batch summaries."""

            assert limit == 100
            return status_rows

        def close(self) -> None:
            """Record that latest_published_csv released the query resource."""

            database_closed.append(True)

    # config 只提供 latest_published_csv 所需的数据库路径边界。
    config = SimpleNamespace(database=SimpleNamespace(path=tmp_path / "collector.db"))
    # 测试不执行 Alembic，只验证 GUI 的发布筛选规则。
    monkeypatch.setattr(gui_module, "upgrade_database", lambda database_path: None)
    monkeypatch.setattr(gui_module, "Database", FakeDatabase)

    assert latest_published_csv(config) == partial_csv  # type: ignore[arg-type]
    assert database_closed == [True]
