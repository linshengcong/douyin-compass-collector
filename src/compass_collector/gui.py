"""PySide6 desktop control console for manual runs and an owned Scheduler."""

import json
import sys
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from compass_collector.config import AppConfig, load_config
from compass_collector.local_data import clear_local_data_with_locks
from compass_collector.notifier import BatchSource
from compass_collector.persistence import Database, upgrade_database
from compass_collector.run_control import CollectionControl
from compass_collector.runner import run_collection
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy, lock_is_held
from compass_collector.runtime_logging import (
    EVENT_STREAM_PREFIX,
    EVENT_STREAM_PATH_ENV,
    read_latest_batch_events,
)
from compass_collector.app_paths import runtime_root, scheduler_process_command
from compass_collector.scheduler_control import (
    SCHEDULER_CONTROL_ID_ENV,
    SchedulerControlFiles,
)


# GUI、Scheduler 与采集锁在桌面版均位于用户应用数据目录，永不进入安装包。
RUNTIME_ROOT = runtime_root()
# GUI 日志表限制内存事件数，持久记录仍以 JSONL 为准。
MAX_VISIBLE_EVENTS = 2000


def read_scheduler_event_file(
    event_path: Path,
    offset: int,
    buffered_text: str,
) -> tuple[list[dict[str, Any]], int, str]:
    """Read complete safe Scheduler event lines appended since one file offset."""

    try:
        with event_path.open("r", encoding="utf-8") as event_file:
            event_file.seek(offset)
            # appended_text 只包含本次定时轮询新增的字节。
            appended_text = event_file.read()
            next_offset = event_file.tell()
    except FileNotFoundError:
        return [], offset, buffered_text
    # 跨写入边界的半行留到下一个轮询，避免 JSON 解析误报。
    complete_lines = (buffered_text + appended_text).split("\n")
    next_buffer = complete_lines.pop()
    # 安全事件已由 Scheduler RuntimeLogger 审核，此处仅校验 JSON 结构。
    events: list[dict[str, Any]] = []
    for line in complete_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, next_offset, next_buffer


class RunMode(str, Enum):
    """Represent stable manual collection modes shown in the GUI."""

    # 正式采集会发布 SQLite 和 CSV。
    OFFICIAL = "official"
    # 试运行只采集和校验，不发布正式数据。
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class GuiLaunchRequest:
    """Describe how one CLI command should initialize the control console."""

    # 配置路径在每次任务开始时重新读取和校验。
    config_path: Path
    # task_id 限定当前控制台管理的任务。
    task_id: str | None
    # auto_start 区分 make app 与 make run/dry-run/force。
    auto_start: bool
    # dry_run 保留 SQLite 审计，但不发布正式商品和 CSV。
    dry_run: bool = False
    # force 对应忽略当天成功幂等记录的高级开关。
    force: bool = False
    # lock_mode 让命令启动的 GUI 不允许临时切换模式。
    lock_mode: bool = False


@dataclass(frozen=True, slots=True)
class CategoryProgress:
    """Represent one independently advancing category in a concurrent collection."""

    # category_run_id 是并发日志事件唯一可靠的归属键。
    category_run_id: str
    # discovery_order 保持 GUI 列表与分类发现顺序一致。
    discovery_order: int
    # category_path 用于让并行进度行可直接识别业务分类。
    category_path: str | None
    # page_no 是已安全持久化的最新分页位置。
    page_no: int = 0
    # target_pages 在该分类首个成功分页后才可准确得到。
    target_pages: int = 0
    # status 仅用于区分活动、成功和失败分类的总量归集。
    status: str = "active"


@dataclass(frozen=True, slots=True)
class GuiProgressState:
    """Represent dynamic category progress independently from Qt widgets."""

    # stage_text 是进度区左侧的当前阶段或分类路径。
    stage_text: str = "阶段：等待开始"
    # progress_text 同时展示分类序号和当前分类分页。
    progress_text: str = "分类 0 / 0 · 第 0 / 0 页"
    # category_index 是当前处理的三级分类发现序号。
    category_index: int = 0
    # category_total 来自本次分类树动态发现结果。
    category_total: int = 0
    # page_no 是当前分类最后成功保存或失败的页码。
    page_no: int = 0
    # target_pages 是当前分类根据实时 total 计算出的页数。
    target_pages: int = 0
    # category_path 保存当前三级分类完整路径。
    category_path: str | None = None
    # result_text 为 None 时不覆盖结果区已有文案。
    result_text: str | None = None
    # csv_path 只保存最近正式发布且可打开的 CSV。
    csv_path: Path | None = None
    # indeterminate 表示分类树请求等尚无可量化总量的阶段。
    indeterminate: bool = False
    # category_progress 按分类运行 ID 保留并发分类各自的分页进度。
    category_progress: tuple[CategoryProgress, ...] = ()
    # completed_category_count 是成功和失败分类之和，构成准确总体分类进度。
    completed_category_count: int = 0


def _event_non_negative_int(
    event: dict[str, Any],
    field: str,
    fallback: int,
) -> int:
    """Read one safe event integer without letting malformed values break the GUI."""

    # raw_value 来自 RuntimeLogger 已脱敏的结构化字段。
    raw_value = event.get(field)
    if isinstance(raw_value, bool):
        return fallback
    try:
        # parsed_value 统一兼容 JSON 数字和数字字符串。
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return fallback
    return parsed_value if parsed_value >= 0 else fallback


def _business_batch_id(event: dict[str, Any]) -> str | None:
    """Return only a task-scoped business batch ID for GUI display."""

    # raw_batch_id 可能来自旧格式通知事件，不能单独证明是业务批次。
    raw_batch_id = event.get("batch_id")
    # raw_task_id 是业务任务日志与执行级通知日志的稳定区分字段。
    raw_task_id = event.get("task_id")
    if not isinstance(raw_batch_id, str) or not raw_batch_id:
        return None
    if not isinstance(raw_task_id, str) or not raw_task_id:
        return None
    return raw_batch_id


def _category_progress_text(
    category_index: int,
    category_total: int,
    page_no: int,
    target_pages: int,
) -> str:
    """Format one compact category and page progress summary."""

    # category_text 在分类树完成后始终显示动态总数。
    category_text = f"分类 {category_index} / {category_total}"
    if target_pages > 0:
        return f"{category_text} · 第 {page_no} / {target_pages} 页"
    if page_no > 0:
        return f"{category_text} · 第 {page_no} 页"
    return f"{category_text} · 等待第 1 页"


def reduce_gui_progress(
    state: GuiProgressState,
    event: dict[str, Any],
) -> GuiProgressState:
    """Reduce safe events into legacy text plus concurrent category progress state."""

    # 旧单分类文本继续服务于状态摘要，独立分类进度由第二步补充。
    legacy_state = _reduce_gui_progress_legacy(state, event)
    return _reduce_concurrent_category_progress(legacy_state, event)


def _reduce_gui_progress_legacy(
    state: GuiProgressState,
    event: dict[str, Any],
) -> GuiProgressState:
    """Reduce one sanitized runtime event into deterministic GUI progress."""

    # event_name 是动态分类链路的稳定事件枚举。
    event_name = str(event.get("event") or "")
    if event_name == "category_batch_started":
        return replace(
            state,
            stage_text="阶段：请求分类树",
            progress_text="正在发现全部一级分类下的三级分类",
            category_index=0,
            category_total=0,
            page_no=0,
            target_pages=0,
            category_path=None,
            result_text="正在请求分类树",
            indeterminate=True,
        )
    if event_name == "category_discovery_succeeded":
        # category_total 每次任务都以当次分类接口返回为准。
        category_total = _event_non_negative_int(
            event,
            "discovered_category_count",
            state.category_total,
        )
        return replace(
            state,
            stage_text="阶段：分类发现完成",
            progress_text=f"已发现 {category_total} 个三级分类",
            category_index=0,
            category_total=category_total,
            page_no=0,
            target_pages=0,
            category_path=None,
            result_text="分类发现完成，准备并发采集",
            indeterminate=False,
        )
    if event_name == "category_collection_started":
        # category_index 来自分类树的稳定发现顺序。
        category_index = _event_non_negative_int(
            event,
            "discovery_order",
            state.category_index,
        )
        # raw_category_path 只接受非空字符串，避免展示 None 或容器文本。
        raw_category_path = event.get("category_path")
        # category_path 缺失时沿用上一条安全路径。
        category_path = (
            raw_category_path.strip()
            if isinstance(raw_category_path, str) and raw_category_path.strip()
            else state.category_path
        )
        return replace(
            state,
            stage_text=(
                f"阶段：{category_path}" if category_path else "阶段：采集三级分类"
            ),
            progress_text=_category_progress_text(
                category_index,
                state.category_total,
                0,
                0,
            ),
            category_index=category_index,
            page_no=0,
            target_pages=0,
            category_path=category_path,
            result_text="采集中",
            indeterminate=False,
        )
    if event_name == "category_page_saved":
        # page_no 和 target_pages 只在页面完成三层持久化后推进。
        page_no = _event_non_negative_int(event, "page_no", state.page_no)
        target_pages = _event_non_negative_int(
            event,
            "target_pages",
            state.target_pages,
        )
        return replace(
            state,
            progress_text=_category_progress_text(
                state.category_index,
                state.category_total,
                page_no,
                target_pages,
            ),
            page_no=page_no,
            target_pages=target_pages,
            indeterminate=False,
        )
    if event_name == "category_collection_succeeded":
        # target_pages 允许分类成功事件补齐最后一页进度。
        target_pages = _event_non_negative_int(
            event,
            "target_pages",
            state.target_pages,
        )
        # completed_page 在正常事件序列中等于最后已保存页。
        completed_page = target_pages if target_pages > 0 else state.page_no
        return replace(
            state,
            stage_text=(
                f"阶段：分类完成 · {state.category_path}"
                if state.category_path
                else "阶段：分类完成"
            ),
            progress_text=_category_progress_text(
                state.category_index,
                state.category_total,
                completed_page,
                target_pages,
            ),
            page_no=completed_page,
            target_pages=target_pages,
            result_text="采集中",
            indeterminate=False,
        )
    if event_name == "category_collection_failed":
        # failed_page 可能在第一页响应契约建立前出现，因此保留已有页数。
        failed_page = _event_non_negative_int(event, "page_no", state.page_no)
        # failed_progress 在原进度后明确标记当前分类失败。
        failed_progress = _category_progress_text(
            state.category_index,
            state.category_total,
            failed_page,
            state.target_pages,
        )
        return replace(
            state,
            stage_text=(
                f"阶段：分类失败 · {state.category_path}"
                if state.category_path
                else "阶段：分类失败"
            ),
            progress_text=f"{failed_progress} · 失败",
            page_no=failed_page,
            result_text="当前分类采集失败，继续执行后续分类",
            indeterminate=False,
        )
    if event_name == "category_discovery_failed":
        # 分类树失败时尚无可展示的分类总量或分页进度。
        failure_message = str(event.get("message") or "分类发现失败")
        return replace(
            state,
            stage_text="阶段：分类发现失败",
            progress_text="未进入三级分类采集",
            result_text=failure_message,
            indeterminate=False,
        )
    if event_name == "category_batch_collection_terminated":
        # batch_status 区分人工中止、登录失效和不可继续的采集失败。
        batch_status = str(event.get("batch_status") or "failed")
        # result_text 使用固定安全文案，不展示底层异常原文。
        result_text_by_status = {
            "interrupted": "已中止，未发布不完整数据",
            "auth_required": "登录态失效，本次未发布正式数据",
            "abandoned": "分类采集异常终止，本次未发布正式数据",
            "failed": "分类失败达到终止条件，本次未发布正式数据",
        }
        return replace(
            state,
            stage_text="阶段：分类采集已终止",
            result_text=result_text_by_status.get(
                batch_status,
                "分类采集已终止，本次未发布正式数据",
            ),
            indeterminate=False,
        )
    if event_name == "category_batch_collection_ready":
        # category_total 以批次汇总事件补充或校正发现数量。
        category_total = _event_non_negative_int(
            event,
            "discovered_category_count",
            state.category_total,
        )
        return replace(
            state,
            stage_text="阶段：分类采集完成，等待发布",
            progress_text=(
                f"分类 {category_total} / {category_total} · 采集完成，等待发布"
            ),
            category_index=category_total,
            category_total=category_total,
            page_no=0,
            target_pages=0,
            category_path=None,
            result_text="分类采集完成，等待发布",
            indeterminate=False,
        )
    if event_name == "batch_skipped":
        # 手动运行遇到既有成功版本时必须终止 Loading，而非停留在准备状态。
        return replace(
            state,
            stage_text="阶段：本次已跳过",
            progress_text="已有成功版本，未启动浏览器采集",
            result_text="已有成功版本，本次未采集",
            indeterminate=False,
        )
    if event_name == "publication_succeeded":
        # raw_csv_path 由正式发布事件提供，不从普通成功状态推断。
        raw_csv_path = event.get("csv_path")
        # published_csv_path 缺失时保留上一次已发布文件。
        published_csv_path = (
            Path(raw_csv_path.strip())
            if isinstance(raw_csv_path, str) and raw_csv_path.strip()
            else state.csv_path
        )
        # batch_status 区分全量成功和允许发布的少量分类失败。
        batch_status = str(event.get("batch_status") or "")
        # result_text 是发布终态的明确用户提示。
        result_text = (
            "部分分类失败，成功结果已发布"
            if batch_status == "partial_success"
            else "正式采集成功"
        )
        return replace(
            state,
            stage_text="阶段：发布完成",
            progress_text=(
                f"分类 {state.category_total} / {state.category_total} · 发布完成"
                if state.category_total > 0
                else "正式结果已发布"
            ),
            category_index=state.category_total,
            category_path=None,
            result_text=result_text,
            csv_path=published_csv_path,
            indeterminate=False,
        )
    if event_name == "dry_run_succeeded":
        # batch_status 让试运行同样明确暴露少量分类失败。
        batch_status = str(event.get("batch_status") or "")
        # result_text 必须说明试运行从未发布正式数据。
        result_text = (
            "部分分类失败，试运行已完成（未发布正式数据）"
            if batch_status == "partial_success"
            else "试运行成功，未发布正式数据"
        )
        return replace(
            state,
            stage_text="阶段：试运行完成",
            progress_text=(
                f"分类 {state.category_total} / {state.category_total} · 试运行完成"
                if state.category_total > 0
                else "试运行完成"
            ),
            category_index=state.category_total,
            category_path=None,
            result_text=result_text,
            indeterminate=False,
        )
    if event_name == "publication_failed":
        return replace(
            state,
            stage_text="阶段：发布失败",
            progress_text="分类采集已完成，但正式结果未发布",
            result_text="发布失败，未生成正式商品数据或 CSV",
            indeterminate=False,
        )
    if event_name == "authentication_expired":
        return replace(
            state,
            stage_text="阶段：登录态失效",
            result_text="登录态失效，本次未发布正式数据",
            indeterminate=False,
        )
    if event_name in {
        "task_collection_failed",
        "task_internal_failed",
        "browser_operation_failed",
        "authentication_batch_blocked",
        "collection_busy",
        "gui_worker_failed",
    }:
        # failure_message 只展示 RuntimeLogger 已审核的安全摘要。
        failure_message = str(event.get("message") or "采集失败")
        return replace(
            state,
            stage_text="阶段：采集失败",
            result_text=failure_message,
            indeterminate=False,
        )
    if event_name == "batch_interrupted":
        return replace(
            state,
            stage_text="阶段：已中止",
            result_text="已中止，未发布不完整数据",
            indeterminate=False,
        )
    if event_name == "manual_inspection_ready":
        return replace(
            state,
            stage_text="阶段：调试检查",
            indeterminate=False,
        )
    return state


def _event_category_run_id(
    state: GuiProgressState,
    event: dict[str, Any],
) -> str | None:
    """Resolve a category event to its stable ID, retaining single-category legacy logs."""

    # 新事件始终携带上下文 ID；旧单分类事件可安全回退到唯一活动分类。
    raw_value = event.get("category_run_id")
    if isinstance(raw_value, str) and raw_value:
        return raw_value
    active_ids = [
        item.category_run_id
        for item in state.category_progress
        if item.status == "active"
    ]
    return active_ids[0] if len(active_ids) == 1 else None


def _replace_category_progress(
    state: GuiProgressState,
    category_progress: CategoryProgress,
) -> tuple[CategoryProgress, ...]:
    """Upsert one category progress row while keeping discovery order stable."""

    rows = {
        item.category_run_id: item
        for item in state.category_progress
    }
    rows[category_progress.category_run_id] = category_progress
    return tuple(
        sorted(
            rows.values(),
            key=lambda item: (item.discovery_order, item.category_run_id),
        )
    )


def _reduce_concurrent_category_progress(
    state: GuiProgressState,
    event: dict[str, Any],
) -> GuiProgressState:
    """Track interleaved category events without letting concurrent workers overwrite peers."""

    event_name = str(event.get("event") or "")
    if event_name in {"category_batch_started", "category_discovery_succeeded"}:
        # 新批次或刚完成的新分类树都必须清除上一轮并行任务残留。
        return replace(
            state,
            category_progress=(),
            completed_category_count=0,
        )
    if event_name not in {
        "category_collection_started",
        "category_page_saved",
        "category_collection_succeeded",
        "category_collection_failed",
        "category_batch_collection_ready",
    }:
        return state
    if event_name == "category_batch_collection_ready":
        return replace(
            state,
            completed_category_count=state.category_total,
        )

    category_run_id = _event_category_run_id(state, event)
    if category_run_id is None:
        return state
    existing = next(
        (item for item in state.category_progress if item.category_run_id == category_run_id),
        None,
    )
    discovery_order = _event_non_negative_int(
        event,
        "discovery_order",
        existing.discovery_order if existing is not None else state.category_index,
    )
    raw_path = event.get("category_path")
    category_path = (
        raw_path.strip()
        if isinstance(raw_path, str) and raw_path.strip()
        else existing.category_path if existing is not None else state.category_path
    )
    page_no = _event_non_negative_int(
        event,
        "page_no",
        existing.page_no if existing is not None else 0,
    )
    target_pages = _event_non_negative_int(
        event,
        "target_pages",
        existing.target_pages if existing is not None else 0,
    )
    status = "active"
    if event_name == "category_collection_succeeded":
        status = "succeeded"
        page_no = target_pages if target_pages > 0 else page_no
    elif event_name == "category_collection_failed":
        status = "failed"
    category_progress = _replace_category_progress(
        state,
        CategoryProgress(
            category_run_id=category_run_id,
            discovery_order=discovery_order,
            category_path=category_path,
            page_no=page_no,
            target_pages=target_pages,
            status=status,
        ),
    )
    completed_category_count = sum(
        1 for item in category_progress if item.status != "active"
    )
    return replace(
        state,
        category_progress=category_progress,
        completed_category_count=completed_category_count,
    )


class CollectionWorker(QObject):
    """Run synchronous Playwright and HTTPX work entirely inside one QThread."""

    # 安全事件跨线程传给主窗口，payload 已由 RuntimeLogger 审核。
    event_received = Signal(dict)
    # 完成信号只传退出码和稳定异常类型，不传异常原文。
    finished = Signal(int, str)

    def __init__(
        self,
        request: GuiLaunchRequest,
        mode: RunMode,
        force: bool,
        control: CollectionControl,
    ) -> None:
        """Bind one immutable GUI request to its cooperative control object."""

        super().__init__()
        # request 保存配置和任务入口，不包含认证值。
        self.request = request
        # mode 决定是否发布正式商品和 CSV，dry-run 仍保留 SQLite 审计。
        self.mode = mode
        # force 只影响正式运行幂等版本分配。
        self.force = force
        # control 由主线程发出停止或关闭 Chrome 信号。
        self.control = control

    @Slot()
    def run(self) -> None:
        """Reload config and execute one collection without touching GUI widgets."""

        try:
            # 每次点击开始都重新读取只读配置，确保展示与执行边界清晰。
            config = load_config(self.request.config_path)
            # runner 在本工作线程创建并关闭 Playwright、HTTPX 和数据库资源。
            exit_code = run_collection(
                config,
                self.request.task_id,
                force=self.force,
                dry_run=self.mode is RunMode.DRY_RUN,
                control=self.control,
                run_source=BatchSource.GUI,
            )
        except RuntimeLockBusy:
            # 锁冲突只输出固定安全摘要，不读取其他进程命令行。
            self.event_received.emit(
                _local_event(
                    level="WARNING",
                    event="collection_busy",
                    message="Chrome 正被其他登录或采集任务使用",
                    stage="coordination",
                )
            )
            self.finished.emit(1, "RuntimeLockBusy")
            return
        except Exception as error:
            # 未预期异常只传类型，详细敏感上下文不进入 GUI。
            self.event_received.emit(
                _local_event(
                    level="ERROR",
                    event="gui_worker_failed",
                    message=f"采集工作线程启动失败：{type(error).__name__}",
                    stage="gui",
                )
            )
            self.finished.emit(1, type(error).__name__)
            return
        self.finished.emit(exit_code, "")


def _local_event(*, level: str, event: str, message: str, stage: str) -> dict[str, Any]:
    """Create a non-persisted safe GUI coordination event."""

    # 本地协调事件不包含请求、认证或异常原文。
    return {
        "timestamp": "",
        "level": level,
        "event": event,
        "message": message,
        "batch_id": None,
        "category_run_id": None,
        "task_id": None,
        "stage": stage,
    }


def latest_published_csv(config: AppConfig) -> Path | None:
    """Return the newest existing formally published CSV from SQLite metadata."""

    # GUI 初次打开允许初始化数据库结构，但不会创建采集记录。
    upgrade_database(config.database.path)
    # 短生命周期查询避免 GUI 长期占用 SQLite 连接。
    database = Database(config.database.path)
    try:
        # 多取少量记录以跳过已经被人工移动的旧 CSV。
        status_rows = database.recent_status(limit=100)
    finally:
        database.close()
    for row in status_rows:
        # published_at 是正式发布唯一判据，包含 partial_success 并排除 dry-run。
        if row.published_at is None or row.csv_path is None:
            continue
        # 数据库只提供仓库本地输出路径，不接受界面输入路径。
        csv_path = Path(row.csv_path)
        if csv_path.exists():
            return csv_path
    return None


class CollectorWindow(QMainWindow):
    """Own the single-window collection state machine and user actions."""

    def __init__(self, config: AppConfig, request: GuiLaunchRequest) -> None:
        """Build an idle console and optionally schedule one immediate run."""

        super().__init__()
        # config 只用于展示和初次校验，任务开始时会重新读取。
        self.config = config
        # request 记录 CLI 启动语义。
        self.request = request
        # 当前工作线程为空表示没有 GUI 手动采集。
        self.collection_thread: QThread | None = None
        # worker 必须保留强引用直到 QThread 完成。
        self.collection_worker: CollectionWorker | None = None
        # control 是中止和关闭 Chrome 的线程安全边界。
        self.collection_control: CollectionControl | None = None
        # collecting 表示采集或保留 Chrome 检查期仍占有执行锁。
        self.collecting = False
        # inspection_ready 表示可安全点击“关闭 Chrome”。
        self.inspection_ready = False
        # pending_close 表示用户已确认清理后退出窗口。
        self.pending_close = False
        # owned_scheduler 表示 QProcess 由本窗口启动并负责停止。
        self.owned_scheduler = False
        # scheduler_job_active 用于区分停止调度与中止当前定时采集。
        self.scheduler_job_active = False
        # scheduler_control 只寻址本窗口本次启动的 Scheduler 子进程。
        self.scheduler_control: SchedulerControlFiles | None = None
        # Scheduler 标准输出可能跨 readyRead 信号分段。
        self.scheduler_buffer = ""
        # Scheduler 事件文件读取到的位置只属于当前受控子进程。
        self.scheduler_event_offset = 0
        # 事件文件最后的半行留给下一次轮询补全。
        self.scheduler_event_buffer = ""
        # 当前或最近已发布 CSV 是打开按钮的唯一目标。
        self.current_csv_path = latest_published_csv(config)
        # progress_state 让事件处理逻辑可以脱离真实 Qt 窗口单独验证。
        self.progress_state = GuiProgressState(csv_path=self.current_csv_path)
        # 所有展示事件只保存在内存，超过上限丢弃最旧项。
        self.events: list[dict[str, Any]] = []
        # Scheduler 子进程由 Qt 管理生命周期和输出读取。
        self.scheduler_process = QProcess(self)
        self.scheduler_process.setProcessChannelMode(QProcess.MergedChannels)
        self.scheduler_process.readyReadStandardOutput.connect(self._read_scheduler_output)
        self.scheduler_process.finished.connect(self._scheduler_finished)
        # 状态计时器检测终端或 launchd 启动的外部 Scheduler。
        self.scheduler_timer = QTimer(self)
        self.scheduler_timer.setInterval(2000)
        self.scheduler_timer.timeout.connect(self._refresh_scheduler_status)
        # windowed Scheduler 无 stdout，短周期读取其独立安全事件文件。
        self.scheduler_event_timer = QTimer(self)
        self.scheduler_event_timer.setInterval(200)
        self.scheduler_event_timer.timeout.connect(self._read_scheduler_event_file)
        self._build_ui()
        self._load_persisted_events()
        self._refresh_csv_controls()
        self._refresh_scheduler_status()
        self.scheduler_timer.start()
        self.scheduler_event_timer.start()
        if request.auto_start:
            # 零延迟让窗口先完成展示，再进入可能弹确认框的启动流程。
            QTimer.singleShot(0, self.start_collection)

    def _build_ui(self) -> None:
        """Create the confirmed single-window status, log, and action layout."""

        self.setWindowTitle("抖音罗盘采集控制台")
        self.resize(1080, 720)
        # central_widget 承载所有状态和操作区，不创建额外业务窗口。
        central_widget = QWidget(self)
        # root_layout 按状态、进度、日志、结果、操作从上到下排列。
        root_layout = QVBoxLayout(central_widget)
        self.setCentralWidget(central_widget)

        # 顶部状态区展示只读任务与运行环境摘要。
        status_group = QGroupBox("运行状态")
        status_layout = QGridLayout(status_group)
        # selected_task 用于展示配置中的人类可读名称。
        selected_task = next(
            (task for task in self.config.tasks if task.id == self.request.task_id),
            None,
        )
        if selected_task is None and self.request.task_id is not None:
            raise ValueError(f"enabled task not found: {self.request.task_id}")
        if selected_task is None:
            # 未指定 task_id 时，空闲控制台展示并执行全部启用任务。
            selected_task = next(
                (task for task in self.config.tasks if task.enabled),
                None,
            )
        if selected_task is None:
            raise ValueError("no enabled tasks are configured")
        self.task_label = QLabel(f"{selected_task.display_name} ({selected_task.id})")
        self.account_label = QLabel(f"当前 Profile：{self.config.browser.profile_dir}")
        self.host_label = QLabel("compass.jinritemai.com")
        self.interval_label = QLabel(
            f"{self.config.http.request_interval_seconds.min:g}–"
            f"{self.config.http.request_interval_seconds.max:g} 秒 / "
            f"一级分类 {self.config.http.level1_concurrency} 线程、"
            f"分页 {self.config.http.page_concurrency} 线程、"
            f"全局 {self.config.http.max_in_flight_requests} 请求"
        )
        self.schedule_label = QLabel(selected_task.schedule)
        self.run_status_label = QLabel("空闲")
        self.scheduler_status_label = QLabel("未运行")
        status_layout.addWidget(QLabel("任务"), 0, 0)
        status_layout.addWidget(self.task_label, 0, 1)
        status_layout.addWidget(QLabel("账号"), 0, 2)
        status_layout.addWidget(self.account_label, 0, 3)
        status_layout.addWidget(QLabel("主机"), 1, 0)
        status_layout.addWidget(self.host_label, 1, 1)
        status_layout.addWidget(QLabel("请求间隔"), 1, 2)
        status_layout.addWidget(self.interval_label, 1, 3)
        status_layout.addWidget(QLabel("定时规则"), 2, 0)
        status_layout.addWidget(self.schedule_label, 2, 1)
        status_layout.addWidget(QLabel("采集状态"), 2, 2)
        status_layout.addWidget(self.run_status_label, 2, 3)
        status_layout.addWidget(QLabel("Scheduler"), 3, 0)
        status_layout.addWidget(self.scheduler_status_label, 3, 1, 1, 3)
        root_layout.addWidget(status_group)

        # 进度区展示准确分类完成度和固定槽位的并发分类明细。
        progress_group = QGroupBox("采集进度")
        progress_layout = QVBoxLayout(progress_group)
        self.stage_label = QLabel(self.progress_state.stage_text)
        progress_layout.addWidget(self.stage_label)
        category_progress_layout = QHBoxLayout()
        self.category_progress_label = QLabel("总体分类")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_text = QLabel(self.progress_state.progress_text)
        category_progress_layout.addWidget(self.category_progress_label)
        category_progress_layout.addWidget(self.progress_bar, 1)
        category_progress_layout.addWidget(self.progress_text)
        progress_layout.addLayout(category_progress_layout)
        self.active_category_progress_layout = QVBoxLayout()
        # 槽位数量与一级分类并发数一致，后续更新内容而不增删行以避免窗口跳动。
        self.active_category_progress_rows: list[
            tuple[QLabel, QProgressBar, QLabel]
        ] = []
        for slot_index in range(max(1, self.config.http.level1_concurrency)):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            category_label = QLabel(f"并发槽位 {slot_index + 1} · 等待分类")
            category_bar = QProgressBar()
            category_bar.setRange(0, 1)
            category_bar.setValue(0)
            page_label = QLabel("空闲")
            row_layout.addWidget(category_label, 1)
            row_layout.addWidget(category_bar, 1)
            row_layout.addWidget(page_label)
            self.active_category_progress_layout.addWidget(row)
            self.active_category_progress_rows.append(
                (category_label, category_bar, page_label)
            )
        progress_layout.addLayout(self.active_category_progress_layout)
        root_layout.addWidget(progress_group)

        # 日志工具栏只筛选内存显示，不修改 JSONL。
        log_toolbar = QHBoxLayout()
        log_toolbar.addWidget(QLabel("日志级别"))
        self.level_filter = QComboBox()
        self.level_filter.addItems(["全部", "INFO", "WARNING", "ERROR"])
        self.level_filter.currentTextChanged.connect(self._render_events)
        self.copy_log_button = QPushButton("复制所选内容")
        self.copy_log_button.clicked.connect(self._copy_selected_logs)
        log_toolbar.addWidget(self.level_filter)
        log_toolbar.addStretch(1)
        log_toolbar.addWidget(self.copy_log_button)
        root_layout.addLayout(log_toolbar)
        # 四列日志表只展示时间、级别、阶段和安全消息。
        self.log_table = QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(["时间", "级别", "阶段", "消息"])
        self.log_table.horizontalHeader().setStretchLastSection(True)
        self.log_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.log_table.setEditTriggers(QTableWidget.NoEditTriggers)
        root_layout.addWidget(self.log_table, 1)

        # 结果区明确区分批次、耗时替代字段和 CSV 目标。
        result_group = QGroupBox("运行结果")
        result_layout = QGridLayout(result_group)
        self.batch_label = QLabel("-")
        self.result_label = QLabel("尚未运行")
        self.notification_label = QLabel("未知")
        self.csv_label = QLabel(str(self.current_csv_path) if self.current_csv_path else "-")
        self.csv_label.setTextInteractionFlags(self.csv_label.textInteractionFlags())
        result_layout.addWidget(QLabel("批次 ID"), 0, 0)
        result_layout.addWidget(self.batch_label, 0, 1)
        result_layout.addWidget(QLabel("结果"), 1, 0)
        result_layout.addWidget(self.result_label, 1, 1)
        result_layout.addWidget(QLabel("CSV"), 2, 0)
        result_layout.addWidget(self.csv_label, 2, 1)
        result_layout.addWidget(QLabel("钉钉通知"), 3, 0)
        result_layout.addWidget(self.notification_label, 3, 1)
        root_layout.addWidget(result_group)

        # 底部模式和操作按钮统一由状态机更新 enabled 状态。
        action_layout = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("正式采集", RunMode.OFFICIAL.value)
        self.mode_combo.addItem(
            "试运行（保留审计，不发布正式商品/CSV）",
            RunMode.DRY_RUN.value,
        )
        self.force_checkbox = QCheckBox("每次自动新版本")
        # 命令启动的 GUI 锁定对应模式，make app 才允许选择。
        initial_mode = RunMode.DRY_RUN if self.request.dry_run else RunMode.OFFICIAL
        self.mode_combo.setCurrentIndex(1 if initial_mode is RunMode.DRY_RUN else 0)
        self.force_checkbox.setChecked(True)
        self.mode_combo.setEnabled(not self.request.lock_mode)
        self.force_checkbox.setEnabled(False)
        self.start_button = QPushButton("开始采集")
        self.start_button.clicked.connect(self.start_collection)
        self.abort_button = QPushButton("中止本次采集")
        self.abort_button.clicked.connect(self.abort_current_collection)
        self.close_chrome_button = QPushButton("完成检查并关闭 Chrome")
        self.close_chrome_button.clicked.connect(self.close_retained_chrome)
        self.scheduler_button = QPushButton("启动 Scheduler")
        self.scheduler_button.clicked.connect(self.toggle_scheduler)
        self.open_csv_button = QPushButton("打开最近已发布 CSV")
        self.open_csv_button.clicked.connect(self.open_csv)
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.clicked.connect(self.open_output_directory)
        self.clear_data_button = QPushButton("清除本地采集数据")
        self.clear_data_button.clicked.connect(self.clear_local_collection_data)
        action_layout.addWidget(self.mode_combo)
        action_layout.addWidget(self.force_checkbox)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.abort_button)
        action_layout.addWidget(self.close_chrome_button)
        action_layout.addWidget(self.scheduler_button)
        action_layout.addWidget(self.open_csv_button)
        action_layout.addWidget(self.open_output_button)
        action_layout.addWidget(self.clear_data_button)
        root_layout.addLayout(action_layout)
        self._update_action_states()

    def _selected_mode(self) -> RunMode:
        """Map the current combo-box data to the stable run mode enum."""

        # Qt item data only contains values inserted by this window.
        return RunMode(self.mode_combo.currentData())

    def _load_persisted_events(self) -> None:
        """Restore the last batch's tail without creating another log source."""

        # 历史事件来自现有安全 JSONL，最多恢复已确认的 500 条。
        persisted_events = read_latest_batch_events(RUNTIME_ROOT / "logs", limit=500)
        for event in persisted_events:
            self._append_event(event, render=False)
        # 倒序恢复最近一个通知终态，不重发任何 Webhook。
        for event in reversed(persisted_events):
            if self._apply_notification_event(event):
                break
        self._render_events()

    @Slot()
    def start_collection(self) -> None:
        """Validate mode confirmation and start one QThread collection worker."""

        if self.collecting:
            return
        # GUI 手动开始始终生成新版本，保证用户主动更新时会进入 Chrome 采集链路。
        force = True
        # control 的事件回调只发 Qt Signal，不直接操作控件。
        self.collection_control = CollectionControl(keep_browser_open=True)
        # worker 绑定当前模式，运行期间界面不允许修改。
        worker = CollectionWorker(
            self.request,
            self._selected_mode(),
            force,
            self.collection_control,
        )
        # RuntimeLogger 在工作线程调用这个安全 Signal。
        self.collection_control.event_sink = worker.event_received.emit
        # 每次运行使用新的 QThread，避免复用已经结束的 Qt 线程对象。
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.event_received.connect(self.handle_event)
        worker.finished.connect(self._collection_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.collection_worker = worker
        self.collection_thread = thread
        self.collecting = True
        self.inspection_ready = False
        self.run_status_label.setText("运行中")
        self.notification_label.setText("等待批次结果")
        # progress_state 每次运行都清空上次分类和分页位置，但保留已发布 CSV。
        self.progress_state = GuiProgressState(
            stage_text="阶段：启动",
            progress_text="正在准备采集",
            result_text="采集中",
            csv_path=self.current_csv_path,
            indeterminate=True,
        )
        self._apply_progress_state()
        self._update_action_states()
        thread.start()

    @Slot(dict)
    def handle_event(self, event: dict[str, Any]) -> None:
        """Update progress and controls from one already-sanitized event."""

        self._append_event(event)
        # event_name 决定采集控制分支，未知事件仍只进入日志表。
        event_name = event.get("event")
        # 当前业务批次 ID 不能被随后到达的通知汇总执行 ID 覆盖。
        batch_id = _business_batch_id(event)
        if batch_id:
            self.batch_label.setText(str(batch_id))
        self._apply_notification_event(event)
        # 纯归约器统一处理动态分类、分页、发布和失败文案。
        self.progress_state = reduce_gui_progress(self.progress_state, event)
        self._apply_progress_state()
        if event_name == "publication_succeeded" and self.current_csv_path is not None:
            # 本次正式发布后按钮立即指向新 CSV，无需等待 Chrome 关闭。
            self.open_csv_button.setText("打开本次 CSV")
        if event_name == "manual_inspection_ready":
            self.collecting = True
            self.inspection_ready = True
            self.run_status_label.setText("等待检查 Chrome")
            self._update_action_states()
        elif event_name == "scheduled_group_started":
            self.scheduler_job_active = True
            self._update_action_states()
        elif event_name == "scheduled_group_finished":
            self.scheduler_job_active = False
            self._update_action_states()

    def _apply_progress_state(self) -> None:
        """Render the pure progress state into the existing Qt controls."""

        self.stage_label.setText(self.progress_state.stage_text)
        self.progress_text.setText(
            f"完成 {self.progress_state.completed_category_count} / "
            f"{self.progress_state.category_total} 个分类"
            if self.progress_state.category_total > 0
            else self.progress_state.progress_text
        )
        if self.progress_state.result_text is not None:
            self.result_label.setText(self.progress_state.result_text)
        if self.progress_state.indeterminate:
            self.progress_bar.setRange(0, 0)
        elif self.progress_state.category_total > 0:
            self.progress_bar.setRange(0, self.progress_state.category_total)
            self.progress_bar.setValue(
                min(
                    self.progress_state.completed_category_count,
                    self.progress_state.category_total,
                )
            )
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
        self._render_active_category_progress()
        # current_csv_path 与纯状态保持一致，禁止从普通成功文案推断 CSV。
        self.current_csv_path = self.progress_state.csv_path
        self.csv_label.setText(str(self.current_csv_path) if self.current_csv_path else "-")
        self.open_csv_button.setEnabled(
            self.current_csv_path is not None and self.current_csv_path.exists()
        )

    def _render_active_category_progress(self) -> None:
        """Update fixed concurrent slots without inserting or removing layout rows."""
        active_categories = [
            item
            for item in self.progress_state.category_progress
            if item.status == "active"
        ]
        for slot_index, (category_label, category_bar, page_label) in enumerate(
            self.active_category_progress_rows
        ):
            category = (
                active_categories[slot_index]
                if slot_index < len(active_categories)
                else None
            )
            if category is None:
                category_label.setText(f"并发槽位 {slot_index + 1} · 等待分类")
                category_bar.setRange(0, 1)
                category_bar.setValue(0)
                page_label.setText("空闲")
                continue
            category_label.setText(
                f"进行中 · {category.category_path or '三级分类'}"
            )
            if category.target_pages > 0:
                category_bar.setRange(0, category.target_pages)
                category_bar.setValue(min(category.page_no, category.target_pages))
                page_label.setText(f"{category.page_no} / {category.target_pages} 页")
            else:
                category_bar.setRange(0, 0)
                page_label.setText("等待首页")

    def _apply_notification_event(self, event: dict[str, Any]) -> bool:
        """Apply one notification lifecycle event and report whether it matched."""

        # 稳定事件名映射为用户可读状态，不展示底层响应。
        notification_states = {
            "notification_pending": "发送中",
            "notification_succeeded": "发送成功",
            "notification_test_succeeded": "测试发送成功",
            "notification_failed": "发送失败",
            "notification_test_failed": "测试发送失败",
            "notification_disabled": "未启用",
        }
        # event_name 只用于匹配本地稳定枚举。
        event_name = event.get("event")
        if event_name not in notification_states:
            return False
        self.notification_label.setText(notification_states[event_name])
        return True

    def _append_event(self, event: dict[str, Any], *, render: bool = True) -> None:
        """Append one safe event to bounded in-memory GUI history."""

        self.events.append(dict(event))
        if len(self.events) > MAX_VISIBLE_EVENTS:
            # 只裁剪界面内存，落盘 JSONL 不受影响。
            del self.events[: len(self.events) - MAX_VISIBLE_EVENTS]
        if render:
            self._render_events()

    @Slot()
    def _render_events(self) -> None:
        """Render the selected safe log level without editing persisted events."""

        # “全部”不应用级别过滤，其余值与日志 level 完全一致。
        selected_level = self.level_filter.currentText()
        visible_events = [
            event
            for event in self.events
            if selected_level == "全部" or event.get("level") == selected_level
        ]
        self.log_table.setRowCount(len(visible_events))
        for row_index, event in enumerate(visible_events):
            # ISO 时间只展示时分秒，空时间用于本地协调事件。
            timestamp = str(event.get("timestamp") or "")
            time_text = timestamp[11:19] if len(timestamp) >= 19 else "-"
            values = (
                time_text,
                str(event.get("level") or ""),
                str(event.get("stage") or ""),
                str(event.get("message") or ""),
            )
            for column_index, value in enumerate(values):
                self.log_table.setItem(row_index, column_index, QTableWidgetItem(value))
        if visible_events:
            self.log_table.scrollToBottom()

    @Slot()
    def _copy_selected_logs(self) -> None:
        """Copy only visible selected safe rows to the system clipboard."""

        # selected_rows 去重后保持表格自然顺序。
        selected_rows = sorted({index.row() for index in self.log_table.selectedIndexes()})
        # 每行使用制表符拼接当前四个安全展示列。
        copied_lines: list[str] = []
        for row_index in selected_rows:
            # 缺失单元格用空字符串代替，不读取隐藏字段。
            values = [
                self.log_table.item(row_index, column_index).text()
                if self.log_table.item(row_index, column_index) is not None
                else ""
                for column_index in range(self.log_table.columnCount())
            ]
            copied_lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(copied_lines))

    @Slot(int, str)
    def _collection_finished(self, exit_code: int, error_type: str) -> None:
        """Release worker references after Chrome and collection resources close."""

        self.collecting = False
        self.inspection_ready = False
        self.run_status_label.setText("完成" if exit_code == 0 else "未成功")
        if exit_code != 0 and self.result_label.text() == "采集中":
            # error_type 只包含稳定 Python 类型名。
            self.result_label.setText(f"运行未成功：{error_type or 'collection_failed'}")
        self.collection_worker = None
        self.collection_thread = None
        self.collection_control = None
        self.progress_bar.setRange(0, max(self.progress_bar.maximum(), 1))
        self._refresh_csv_controls()
        self._update_action_states()
        if self.pending_close:
            QTimer.singleShot(0, self.close)

    @Slot()
    def abort_current_collection(self) -> None:
        """Confirm and cooperatively interrupt a manual or owned scheduled run."""

        # 没有活动任务时按钮不执行任何进程操作。
        if not self.collecting and not self.scheduler_job_active:
            return
        answer = QMessageBox.question(
            self,
            "确认中止",
            "当前请求会在完成或超时后停止，不完整数据不会发布。"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if self.collecting and self.collection_control is not None:
            self.collection_control.request_stop()
            self.run_status_label.setText("正在中止")
        elif self.scheduler_job_active and self.owned_scheduler:
            # 一次性控制文件只中止 Scheduler 当前批次，不停止未来调度。
            if self.scheduler_control is not None:
                self.scheduler_control.request_interruption()

    @Slot()
    def close_retained_chrome(self) -> None:
        """Release the worker's retained Chrome after developer inspection."""

        if self.collection_control is not None and self.inspection_ready:
            self.run_status_label.setText("正在关闭 Chrome")
            # 关闭请求只发送一次，工作线程完成后会进入最终空闲状态。
            self.inspection_ready = False
            self.collection_control.request_browser_close()
            self._update_action_states()

    @Slot()
    def toggle_scheduler(self) -> None:
        """Start an owned QProcess or gracefully stop only that owned process."""

        if self.owned_scheduler:
            self.scheduler_status_label.setText("停止中，等待当前批次完成")
            self.scheduler_button.setEnabled(False)
            # Scheduler 自行停止未来调度并等待当前批次安全收口。
            if self.scheduler_control is not None:
                self.scheduler_control.request_shutdown()
            return
        # 外部 Scheduler 只读展示，永远不从 GUI 终止。
        scheduler_lock_path = RUNTIME_ROOT / "locks" / "scheduler.lock"
        if lock_is_held(scheduler_lock_path, "scheduler"):
            self._refresh_scheduler_status()
            return
        # GUI 子进程使用开发 Python 或已打包应用，并继承同一便携目录。
        process_environment = QProcessEnvironment.systemEnvironment()
        # 每次启动使用全新实例 ID，旧控制文件不能误伤新进程。
        scheduler_control_id = uuid4().hex
        # GUI 和子进程通过相同运行目录解析本次控制文件。
        self.scheduler_control = SchedulerControlFiles(
            RUNTIME_ROOT / "controls",
            scheduler_control_id,
        )
        process_environment.insert(SCHEDULER_CONTROL_ID_ENV, scheduler_control_id)
        process_environment.insert(
            EVENT_STREAM_PATH_ENV,
            str(self.scheduler_control.event_path),
        )
        self.scheduler_process.setProcessEnvironment(process_environment)
        scheduler_program, scheduler_arguments = scheduler_process_command(
            self.request.config_path
        )
        self.scheduler_process.setWorkingDirectory(str(Path.cwd()))
        self.scheduler_process.setProgram(scheduler_program)
        self.scheduler_process.setArguments(scheduler_arguments)
        self.scheduler_buffer = ""
        self.scheduler_event_offset = 0
        self.scheduler_event_buffer = ""
        self.owned_scheduler = True
        self.scheduler_status_label.setText("正在启动")
        self.scheduler_process.start()
        self._update_action_states()

    @Slot()
    def _read_scheduler_output(self) -> None:
        """Decode complete prefixed safe events from the owned QProcess stream."""

        # Qt QByteArray 先转 bytes，跨分段文本保存在 scheduler_buffer。
        output_text = bytes(self.scheduler_process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        self.scheduler_buffer += output_text
        # 最后一段可能没有换行，保留给下一次 readyRead。
        complete_lines = self.scheduler_buffer.split("\n")
        self.scheduler_buffer = complete_lines.pop()
        for line in complete_lines:
            if line.startswith(EVENT_STREAM_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_STREAM_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._handle_scheduler_process_event(event)
            elif line.strip():
                # 子进程非事件输出可能含配置字段，只展示固定安全摘要。
                self._append_event(
                    _local_event(
                        level="ERROR",
                        event="scheduler_process_output",
                        message="Scheduler 子进程启动失败，请在终端检查安全摘要",
                        stage="scheduling",
                    )
                )

    @Slot()
    def _read_scheduler_event_file(self) -> None:
        """Poll the owned windowed Scheduler's instance-scoped event file."""

        scheduler_control = self.scheduler_control
        if scheduler_control is None:
            return
        events, next_offset, next_buffer = read_scheduler_event_file(
            scheduler_control.event_path,
            self.scheduler_event_offset,
            self.scheduler_event_buffer,
        )
        self.scheduler_event_offset = next_offset
        self.scheduler_event_buffer = next_buffer
        for event in events:
            self._handle_scheduler_process_event(event)

    def _handle_scheduler_process_event(self, event: dict[str, Any]) -> None:
        """Apply one Scheduler child event received from a pipe or event file."""

        # 两种传输方式都复用同一 GUI 状态归约，避免平台分叉。
        self.handle_event(event)
        if event.get("event") == "scheduler_started":
            self.scheduler_status_label.setText("GUI Scheduler 运行中")

    @Slot(int, QProcess.ExitStatus)
    def _scheduler_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        """Clear owned Scheduler state after graceful or abnormal process exit."""

        # 进程退出后再读一次，避免漏掉缓冲区中的最后一条完成事件。
        self._read_scheduler_event_file()
        # GUI 负责清理子进程未消费的本实例请求文件。
        finished_control = self.scheduler_control
        if finished_control is not None:
            finished_control.cleanup()
            finished_control.clear_event_log()
        self.scheduler_control = None
        self.owned_scheduler = False
        self.scheduler_job_active = False
        self.scheduler_status_label.setText(
            "已停止" if exit_code == 0 else f"异常退出（code={exit_code}）"
        )
        self._update_action_states()
        if self.pending_close:
            QTimer.singleShot(0, self.close)

    @Slot()
    def _refresh_scheduler_status(self) -> None:
        """Show external Scheduler ownership without granting destructive controls."""

        if self.owned_scheduler:
            self.scheduler_button.setText("停止 Scheduler")
            return
        # advisory lock 是外部实例真相，锁文件中的旧 PID 不参与判断。
        external_running = lock_is_held(
            RUNTIME_ROOT / "locks" / "scheduler.lock",
            "scheduler",
        )
        self.scheduler_status_label.setText(
            "外部 Scheduler 运行中（只读）" if external_running else "未运行"
        )
        self.scheduler_button.setText("启动 Scheduler")
        self._update_action_states(external_scheduler=external_running)

    def _update_action_states(self, *, external_scheduler: bool | None = None) -> None:
        """Derive all button states from collection and Scheduler ownership."""

        if external_scheduler is None:
            # 已拥有子进程时无需额外探测锁，避免短暂状态闪烁。
            external_scheduler = (
                False
                if self.owned_scheduler
                else lock_is_held(RUNTIME_ROOT / "locks" / "scheduler.lock", "scheduler")
            )
        self.start_button.setEnabled(not self.collecting)
        self.mode_combo.setEnabled(not self.collecting and not self.request.lock_mode)
        self.force_checkbox.setEnabled(not self.collecting and not self.request.lock_mode)
        self.abort_button.setEnabled(
            (self.collecting and not self.inspection_ready)
            or (self.scheduler_job_active and self.owned_scheduler)
        )
        self.close_chrome_button.setEnabled(self.inspection_ready)
        self.scheduler_button.setEnabled(
            not self.collecting and (self.owned_scheduler or not external_scheduler)
        )
        self.scheduler_button.setText(
            "停止 Scheduler" if self.owned_scheduler else "启动 Scheduler"
        )
        # 破坏性清理只在采集和所有 Scheduler 都空闲时可用。
        self.clear_data_button.setEnabled(
            not self.collecting
            and not self.owned_scheduler
            and not self.scheduler_job_active
            and not external_scheduler
        )

    def _refresh_csv_controls(self) -> None:
        """Refresh current or latest formally published CSV controls."""

        # 当前文件不存在时重新查询最近仍存在的正式发布 CSV。
        if self.current_csv_path is None or not self.current_csv_path.exists():
            self.current_csv_path = latest_published_csv(self.config)
            self.progress_state = replace(
                self.progress_state,
                csv_path=self.current_csv_path,
            )
            self.open_csv_button.setText("打开最近已发布 CSV")
        self.open_csv_button.setEnabled(self.current_csv_path is not None)
        self.csv_label.setText(str(self.current_csv_path) if self.current_csv_path else "-")

    @Slot()
    def open_csv(self) -> None:
        """Open exactly the displayed CSV with the macOS default application."""

        if self.current_csv_path is None or not self.current_csv_path.exists():
            self._refresh_csv_controls()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_csv_path.resolve())))

    @Slot()
    def open_output_directory(self) -> None:
        """Open the permanent CSV output directory in Finder."""

        # 输出目录允许在首次正式采集前为空。
        output_directory = RUNTIME_ROOT / "exports"
        output_directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_directory.resolve())))

    @Slot()
    def clear_local_collection_data(self) -> None:
        """Confirm and clear allowlisted collection data while preserving login state."""

        # 按钮状态之外再做一次实时检查，覆盖 Scheduler 竞态窗口。
        scheduler_running = self.owned_scheduler or lock_is_held(
            RUNTIME_ROOT / "locks" / "scheduler.lock",
            "scheduler",
        )
        if self.collecting or self.scheduler_job_active or scheduler_running:
            QMessageBox.information(
                self,
                "暂时无法清理",
                "请先结束当前采集并停止 Scheduler。",
            )
            return
        # 确认文案显式列出删除与保留边界。
        answer = QMessageBox.question(
            self,
            "确认清除本地采集数据",
            "此操作不可恢复，将删除 SQLite、CSV、原始响应、"
            "失败材料和 JSONL 日志。\n\n"
            "会保留 Chrome 登录态、.env、配置、运行锁和备份。\n\n"
            "确认继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        # 同步清理期间禁用按钮，完成后由统一状态机恢复。
        self.clear_data_button.setEnabled(False)
        try:
            cleanup_summary = clear_local_data_with_locks(
                RUNTIME_ROOT,
                self.config.database.path,
            )
        except RuntimeLockBusy:
            QMessageBox.warning(
                self,
                "清理未执行",
                "采集或 Scheduler 已开始运行，本次没有清理数据。",
            )
            self._update_action_states()
            return
        except (OSError, ValueError) as error:
            # 只显示稳定异常类型，不暴露本机路径或底层错误原文。
            QMessageBox.critical(
                self,
                "清理失败",
                f"本地数据安全检查失败：{type(error).__name__}",
            )
            self._update_action_states()
            return

        # 数据已可能发生部分删除，无论结果都重置历史展示。
        self.events.clear()
        self._render_events()
        self.current_csv_path = None
        self.progress_state = GuiProgressState()
        self.batch_label.setText("-")
        self.notification_label.setText("未知")
        self.run_status_label.setText("空闲")
        self.result_label.setText("尚未运行")
        self._apply_progress_state()
        self.open_csv_button.setText("打开最近已发布 CSV")
        # 完成弹窗只展示删除计数和失败数量。
        result_message = (
            f"已删除数据库文件 {cleanup_summary.database_files} 个，"
            f"清空数据目录 {cleanup_summary.runtime_directories} 个，"
            f"失败 {cleanup_summary.failures} 项。\n"
            "Chrome 登录态已保留。"
        )
        if cleanup_summary.succeeded:
            QMessageBox.information(self, "清理完成", result_message)
        else:
            QMessageBox.warning(self, "清理未完全成功", result_message)
        self._update_action_states()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Require explicit cleanup of active collection and owned Scheduler work."""

        if self.collecting:
            answer = QMessageBox.question(
                self,
                "采集仍在运行",
                "退出会中止本次采集、关闭 Chrome 并等待资源清理，"
                "是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.pending_close = True
            if self.collection_control is not None:
                self.collection_control.request_stop()
                self.collection_control.request_browser_close()
            self.run_status_label.setText("正在清理后退出")
            event.ignore()
            return
        if self.owned_scheduler:
            answer = QMessageBox.question(
                self,
                "Scheduler 仍在运行",
                "退出会优雅停止 GUI 启动的 Scheduler，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.pending_close = True
            self.scheduler_status_label.setText("停止中，等待当前批次完成")
            # 退出窗口与按钮停止使用相同的等待式跨平台协议。
            if self.scheduler_control is not None:
                self.scheduler_control.request_shutdown()
            event.ignore()
            return
        event.accept()


def run_gui(config: AppConfig, request: GuiLaunchRequest) -> int:
    """Run one process-wide PySide6 console protected by a GUI instance lock."""

    # QApplication 必须先存在，第二实例才能显示可见提示。
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("抖音罗盘采集控制台")
    # GUI 锁只约束窗口实例，不阻止 --no-gui 状态查询。
    gui_lock = ProcessLock(RUNTIME_ROOT / "locks" / "gui.lock", "gui")
    try:
        gui_lock.acquire()
    except RuntimeLockBusy:
        QMessageBox.information(None, "控制台已运行", "采集控制台已经打开。")
        return 1
    try:
        # 主窗口持有所有 Qt 子对象直到应用事件循环退出。
        window = CollectorWindow(config, request)
        window.show()
        return application.exec()
    finally:
        gui_lock.release()
