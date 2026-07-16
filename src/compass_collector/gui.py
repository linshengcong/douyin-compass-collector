"""PySide6 desktop control console for manual runs and an owned Scheduler."""

import json
import os
import signal
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

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
from compass_collector.notifier import BatchSource
from compass_collector.persistence import Database, upgrade_database
from compass_collector.run_control import CollectionControl
from compass_collector.runner import run_collection
from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy, lock_is_held
from compass_collector.runtime_logging import (
    EVENT_STREAM_ENV,
    EVENT_STREAM_PREFIX,
    read_latest_batch_events,
)


# GUI、Scheduler 与采集锁均位于本机 runtime，永不进入仓库。
RUNTIME_ROOT = Path("runtime")
# GUI 日志表限制内存事件数，持久记录仍以 JSONL 为准。
MAX_VISIBLE_EVENTS = 2000


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
    # dry_run 对应 CLI 的无发布模式。
    dry_run: bool = False
    # force 对应忽略当天成功幂等记录的高级开关。
    force: bool = False
    # lock_mode 让命令启动的 GUI 不允许临时切换模式。
    lock_mode: bool = False


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
        # mode 决定是否发布正式 SQLite/CSV。
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
        "run_id": None,
        "task_id": None,
        "stage": stage,
    }


def latest_success_csv(config: AppConfig) -> Path | None:
    """Return the newest existing successful CSV from SQLite status metadata."""

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
        if row.status != "success" or row.csv_path is None:
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
        # Scheduler 标准输出可能跨 readyRead 信号分段。
        self.scheduler_buffer = ""
        # 当前或最近成功 CSV 是打开按钮的唯一目标。
        self.current_csv_path = latest_success_csv(config)
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
        self._build_ui()
        self._load_persisted_events()
        self._refresh_csv_controls()
        self._refresh_scheduler_status()
        self.scheduler_timer.start()
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
            f"{self.config.http.page_interval_seconds.min:g}–"
            f"{self.config.http.page_interval_seconds.max:g} 秒 / 串行"
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

        # 进度区由结构化 page_collected 事件驱动。
        progress_layout = QHBoxLayout()
        self.stage_label = QLabel("阶段：等待开始")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_text = QLabel("0 / 0 页")
        progress_layout.addWidget(self.stage_label)
        progress_layout.addWidget(self.progress_bar, 1)
        progress_layout.addWidget(self.progress_text)
        root_layout.addLayout(progress_layout)

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
        self.mode_combo.addItem("试运行（不发布 SQLite/CSV）", RunMode.DRY_RUN.value)
        self.force_checkbox = QCheckBox("强制新版本")
        # 命令启动的 GUI 锁定对应模式，make app 才允许选择。
        initial_mode = RunMode.DRY_RUN if self.request.dry_run else RunMode.OFFICIAL
        self.mode_combo.setCurrentIndex(1 if initial_mode is RunMode.DRY_RUN else 0)
        self.force_checkbox.setChecked(self.request.force)
        self.mode_combo.setEnabled(not self.request.lock_mode)
        self.force_checkbox.setEnabled(not self.request.lock_mode)
        self.start_button = QPushButton("开始采集")
        self.start_button.clicked.connect(self.start_collection)
        self.abort_button = QPushButton("中止本次采集")
        self.abort_button.clicked.connect(self.abort_current_collection)
        self.close_chrome_button = QPushButton("完成检查并关闭 Chrome")
        self.close_chrome_button.clicked.connect(self.close_retained_chrome)
        self.scheduler_button = QPushButton("启动 Scheduler")
        self.scheduler_button.clicked.connect(self.toggle_scheduler)
        self.open_csv_button = QPushButton("打开最近成功 CSV")
        self.open_csv_button.clicked.connect(self.open_csv)
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.clicked.connect(self.open_output_directory)
        action_layout.addWidget(self.mode_combo)
        action_layout.addWidget(self.force_checkbox)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.abort_button)
        action_layout.addWidget(self.close_chrome_button)
        action_layout.addWidget(self.scheduler_button)
        action_layout.addWidget(self.open_csv_button)
        action_layout.addWidget(self.open_output_button)
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
        # force 每次启动都需要明确二次确认，避免误生成新版本。
        force = self.force_checkbox.isChecked()
        if force:
            answer = QMessageBox.question(
                self,
                "确认强制采集",
                "强制采集会忽略本计划时间已有成功记录并创建新版本，"
                "是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
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
        self.result_label.setText("采集中")
        self.notification_label.setText("等待批次结果")
        self.stage_label.setText("阶段：启动")
        self.progress_bar.setRange(0, 0)
        self._update_action_states()
        thread.start()

    @Slot(dict)
    def handle_event(self, event: dict[str, Any]) -> None:
        """Update progress and controls from one already-sanitized event."""

        self._append_event(event)
        # event_name 决定状态机分支，未知事件只进入日志表。
        event_name = event.get("event")
        # 当前批次 ID 从第一个带上下文的事件开始展示。
        batch_id = event.get("batch_id")
        if batch_id:
            self.batch_label.setText(str(batch_id))
        self._apply_notification_event(event)
        if event_name == "task_started":
            self.stage_label.setText("阶段：采集")
        elif event_name == "page_collected":
            # 页码字段来自 RuntimeLogger allowlist，可安全转为整数。
            page_no = int(event.get("page_no", 0))
            target_pages = int(event.get("target_pages", 0))
            self.progress_bar.setRange(0, max(target_pages, 1))
            self.progress_bar.setValue(page_no)
            self.progress_text.setText(f"{page_no} / {target_pages} 页")
        elif event_name == "publication_succeeded":
            # 正式发布事件携带确切 CSV 路径，优先于历史成功文件。
            csv_value = event.get("csv_path")
            if isinstance(csv_value, str):
                self.current_csv_path = Path(csv_value)
                self.csv_label.setText(csv_value)
                self.open_csv_button.setText("打开本次 CSV")
            self.result_label.setText("正式采集成功")
        elif event_name == "dry_run_succeeded":
            self.result_label.setText("试运行成功，未发布正式数据")
        elif event_name in {
            "task_collection_failed",
            "task_internal_failed",
            "browser_operation_failed",
            "authentication_batch_blocked",
        }:
            self.result_label.setText(str(event.get("message", "采集失败")))
        elif event_name == "batch_interrupted":
            self.result_label.setText("已中止，未发布不完整数据")
        elif event_name == "manual_inspection_ready":
            self.collecting = True
            self.inspection_ready = True
            self.run_status_label.setText("等待检查 Chrome")
            self.stage_label.setText("阶段：调试检查")
            self._update_action_states()
        elif event_name == "scheduled_group_started":
            self.scheduler_job_active = True
            self._update_action_states()
        elif event_name == "scheduled_group_finished":
            self.scheduler_job_active = False
            self._update_action_states()

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
            # SIGUSR1 只转发给 Scheduler 当前批次，不停止未来调度。
            scheduler_pid = int(self.scheduler_process.processId())
            if scheduler_pid > 0:
                os.kill(scheduler_pid, signal.SIGUSR1)

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
            self.scheduler_process.terminate()
            return
        # 外部 Scheduler 只读展示，永远不从 GUI 终止。
        scheduler_lock_path = RUNTIME_ROOT / "locks" / "scheduler.lock"
        if lock_is_held(scheduler_lock_path, "scheduler"):
            self._refresh_scheduler_status()
            return
        # GUI 子进程使用当前虚拟环境 Python 和相同工程目录。
        process_environment = QProcessEnvironment.systemEnvironment()
        process_environment.insert(EVENT_STREAM_ENV, "1")
        self.scheduler_process.setProcessEnvironment(process_environment)
        self.scheduler_process.setWorkingDirectory(str(Path.cwd()))
        self.scheduler_process.setProgram(sys.executable)
        self.scheduler_process.setArguments(
            [
                "-m",
                "compass_collector",
                "scheduler",
                "--config",
                str(self.request.config_path),
            ]
        )
        self.scheduler_buffer = ""
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
                    self.handle_event(event)
                    if event.get("event") == "scheduler_started":
                        self.scheduler_status_label.setText("GUI Scheduler 运行中")
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

    @Slot(int, QProcess.ExitStatus)
    def _scheduler_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        """Clear owned Scheduler state after graceful or abnormal process exit."""

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

    def _refresh_csv_controls(self) -> None:
        """Refresh current or latest successful CSV button state and label."""

        # 当前文件不存在时重新查询最近仍存在的成功 CSV。
        if self.current_csv_path is None or not self.current_csv_path.exists():
            self.current_csv_path = latest_success_csv(self.config)
            self.open_csv_button.setText("打开最近成功 CSV")
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
            self.scheduler_process.terminate()
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
