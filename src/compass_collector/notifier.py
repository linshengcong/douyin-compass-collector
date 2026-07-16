"""Safe DingTalk batch summaries, signed Webhook delivery, and dotenv loading."""

import base64
import hashlib
import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from compass_collector.runtime_logging import LogContext, RuntimeLogger


# 通知时间与采集业务时间统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 第一版只允许钉钉官方自定义机器人地址。
DINGTALK_WEBHOOK_HOST = "oapi.dingtalk.com"
# 自定义机器人固定发送路径不允许由配置扩展。
DINGTALK_WEBHOOK_PATH = "/robot/send"
# 单条 Markdown 使用保守的 UTF-8 字节上限。
MAX_MARKDOWN_BYTES = 8 * 1024
# 环境变量名集中维护，任何值都不得写入日志。
DINGTALK_ENABLED_ENV = "DINGTALK_ENABLED"
DINGTALK_WEBHOOK_ENV = "DINGTALK_WEBHOOK_URL"
DINGTALK_SECRET_ENV = "DINGTALK_SECRET"


class BatchSource(str, Enum):
    """Identify the process surface that initiated a collection batch."""

    GUI = "gui"
    TERMINAL = "terminal"
    SCHEDULER = "scheduler"


class BatchMode(str, Enum):
    """Identify publication semantics for a notification batch."""

    OFFICIAL = "official"
    FORCE = "force"
    DRY_RUN = "dry_run"


class TaskNotificationStatus(str, Enum):
    """Represent one task outcome without exposing internal exception text."""

    SUCCESS = "success"
    FAILED = "failed"
    AUTH_REQUIRED = "auth_required"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"
    SKIPPED_BUSY = "skipped_busy"
    MISSED = "missed"
    NOT_STARTED = "not_started"


class BatchNotificationStatus(str, Enum):
    """Represent the stable aggregate state shown in the Markdown title."""

    SUCCESS = "success"
    DRY_RUN_SUCCESS = "dry_run_success"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"
    NOT_COLLECTED = "not_collected"


class NotificationDeliveryStatus(str, Enum):
    """Represent delivery independently from the collection terminal state."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class DingTalkSettings:
    """Hold process-local notification configuration without logging secrets."""

    # enabled 是显式总开关，不根据凭证存在与否自动开启。
    enabled: bool
    # webhook_url 包含 access_token，只能留在当前进程内存。
    webhook_url: str | None
    # secret 是机器人加签密钥，只能留在当前进程内存。
    secret: str | None
    # error_category 是唯一可进入日志的配置诊断。
    error_category: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether enabled settings can safely send a request."""

        return (
            self.enabled
            and self.webhook_url is not None
            and self.secret is not None
            and self.error_category is None
        )


@dataclass(frozen=True, slots=True)
class TaskNotificationResult:
    """Describe one task using only fields approved for external notification."""

    # task_id 和 display_name 来自本地受版本管理任务配置。
    task_id: str
    display_name: str
    # status 使用固定枚举，不能携带异常原文。
    status: TaskNotificationStatus
    # saved_pages 和 saved_items 来自安全 Manifest 计数。
    saved_pages: int = 0
    saved_items: int = 0
    # csv_filename 只允许文件名，不允许本机绝对路径。
    csv_filename: str | None = None
    # error_category 使用采集器稳定安全分类。
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class BatchNotificationSummary:
    """Aggregate one explicit runner or Scheduler occurrence into one message."""

    # batch_id 用于与 JSONL 安全事件互相定位。
    batch_id: str
    # source 和 mode 决定通知标题中的运行语义。
    source: BatchSource
    mode: BatchMode
    # 批次时间使用带时区值计算耗时。
    started_at: datetime
    finished_at: datetime
    # tasks 保持配置顺序，消息截断也保持确定性。
    tasks: tuple[TaskNotificationResult, ...]

    @property
    def status(self) -> BatchNotificationStatus:
        """Calculate the agreed aggregate state from task terminal statuses."""

        # 显式中止优先于其他混合结果。
        task_statuses = [task.status for task in self.tasks]
        if TaskNotificationStatus.INTERRUPTED in task_statuses:
            return BatchNotificationStatus.INTERRUPTED
        if task_statuses and all(
            status is TaskNotificationStatus.SKIPPED for status in task_statuses
        ):
            return BatchNotificationStatus.SKIPPED
        not_collected_statuses = {
            TaskNotificationStatus.MISSED,
            TaskNotificationStatus.SKIPPED_BUSY,
        }
        if task_statuses and all(status in not_collected_statuses for status in task_statuses):
            return BatchNotificationStatus.NOT_COLLECTED
        # 已成功和幂等跳过的混合批次不应误报“部分失败”。
        successful_statuses = {
            TaskNotificationStatus.SUCCESS,
            TaskNotificationStatus.SKIPPED,
        }
        if task_statuses and all(
            status in successful_statuses for status in task_statuses
        ):
            return (
                BatchNotificationStatus.DRY_RUN_SUCCESS
                if self.mode is BatchMode.DRY_RUN
                else BatchNotificationStatus.SUCCESS
            )
        # 只有任务真实 success 才计入部分成功数量。
        success_count = sum(
            status is TaskNotificationStatus.SUCCESS for status in task_statuses
        )
        if success_count > 0:
            return BatchNotificationStatus.PARTIAL_FAILURE
        return BatchNotificationStatus.FAILED


@dataclass(frozen=True, slots=True)
class NotificationDeliveryResult:
    """Return one safe delivery result without retaining a response body."""

    # status 与采集结果完全独立。
    status: NotificationDeliveryStatus
    # error_category 是失败时唯一对外诊断。
    error_category: str | None = None
    # status_code 不包含请求或认证信息，可进入安全日志。
    status_code: int | None = None


def load_project_environment(dotenv_path: Path = Path(".env")) -> None:
    """Load project dotenv values without overriding existing process variables."""

    # python-dotenv 处理引号、空白和转义，override=False 保留外部注入优先级。
    load_dotenv(dotenv_path=dotenv_path, override=False)


def load_dingtalk_settings() -> DingTalkSettings:
    """Read and strictly validate DingTalk settings from the process environment."""

    # 开关仅接受明确 true/false，其他内容是安全配置错误。
    enabled_text = os.environ.get(DINGTALK_ENABLED_ENV, "false").strip().lower()
    if enabled_text not in {"true", "false"}:
        return DingTalkSettings(
            enabled=False,
            webhook_url=None,
            secret=None,
            error_category="notification_config_invalid",
        )
    if enabled_text == "false":
        return DingTalkSettings(enabled=False, webhook_url=None, secret=None)
    # 凭证值只保留在局部变量和返回对象中，不进入异常文本。
    webhook_url = os.environ.get(DINGTALK_WEBHOOK_ENV, "").strip()
    secret = os.environ.get(DINGTALK_SECRET_ENV, "").strip()
    if not webhook_url or not secret or not _is_valid_webhook(webhook_url):
        return DingTalkSettings(
            enabled=True,
            webhook_url=None,
            secret=None,
            error_category="notification_config_invalid",
        )
    return DingTalkSettings(enabled=True, webhook_url=webhook_url, secret=secret)


def _is_valid_webhook(webhook_url: str) -> bool:
    """Restrict DingTalk Webhooks to the confirmed official robot endpoint."""

    try:
        # urlparse 不发网络请求，只拆分当前进程中的敏感 URL。
        parsed_url = urlparse(webhook_url)
        query_values = parse_qs(parsed_url.query, keep_blank_values=True)
        # port、username 等属性可能对非法 URL 延迟抛出 ValueError。
        parsed_port = parsed_url.port
        parsed_username = parsed_url.username
        parsed_password = parsed_url.password
    except ValueError:
        return False
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != DINGTALK_WEBHOOK_HOST
        or parsed_port is not None
        or parsed_username is not None
        or parsed_password is not None
        or parsed_url.path != DINGTALK_WEBHOOK_PATH
        or parsed_url.fragment
    ):
        return False
    # 第一版只接受一个 access_token，不接受预置 sign/timestamp 或未知参数。
    return (
        set(query_values) == {"access_token"}
        and len(query_values["access_token"]) == 1
        and bool(query_values["access_token"][0].strip())
    )


def _safe_emit(runtime_logger: RuntimeLogger, **event_fields: Any) -> None:
    """Keep notification logging failures from changing collection outcomes."""

    try:
        # 通知日志是附加观测能力，不能反向破坏采集主链路。
        runtime_logger.emit(**event_fields)
    except Exception:
        # 此处不记录底层异常，避免通知或日志内容可能携带敏感数据。
        return


def create_signature(timestamp_ms: int, secret: str) -> str:
    """Create the DingTalk HMAC-SHA256 Base64 signature before URL encoding."""

    # 协议签名正文由毫秒时间戳、换行和加签密钥组成。
    string_to_sign = f"{timestamp_ms}\n{secret}".encode("utf-8")
    # HMAC 密钥同样使用 UTF-8，返回标准 Base64 文本交给 httpx 编码。
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _escape_markdown_cell(value: str) -> str:
    """Keep configured labels inside one Markdown table cell."""

    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _batch_title(status: BatchNotificationStatus, *, test: bool = False) -> str:
    """Map stable batch states to concise DingTalk Markdown titles."""

    if test:
        return "🧪 罗盘采集器通知测试"
    # 标题映射只包含固定文本，不拼接任务或异常内容。
    titles = {
        BatchNotificationStatus.SUCCESS: "✅ 罗盘采集成功",
        BatchNotificationStatus.DRY_RUN_SUCCESS: "✅ 罗盘试运行成功",
        BatchNotificationStatus.PARTIAL_FAILURE: "⚠️ 罗盘采集部分失败",
        BatchNotificationStatus.FAILED: "❌ 罗盘采集失败",
        BatchNotificationStatus.INTERRUPTED: "⏹️ 罗盘采集已中止",
        BatchNotificationStatus.SKIPPED: "ℹ️ 罗盘采集已跳过",
        BatchNotificationStatus.NOT_COLLECTED: "⚠️ 罗盘计划未采集",
    }
    return titles[status]


def render_batch_markdown(summary: BatchNotificationSummary) -> tuple[str, str]:
    """Render one bounded Markdown summary while preserving essential metadata."""

    # 固定中文标签让群消息无需理解内部枚举。
    source_labels = {
        BatchSource.GUI: "GUI",
        BatchSource.TERMINAL: "终端",
        BatchSource.SCHEDULER: "Scheduler",
    }
    mode_labels = {
        BatchMode.OFFICIAL: "正式采集",
        BatchMode.FORCE: "强制采集",
        BatchMode.DRY_RUN: "试运行",
    }
    # 耗时不小于零，避免系统时间轻微回拨产生负数。
    duration_seconds = max(
        0,
        int((summary.finished_at - summary.started_at).total_seconds()),
    )
    title = _batch_title(summary.status)
    # 必须保留的头部不包含本机路径或认证信息。
    lines = [
        f"### {title}",
        "",
        f"- 来源：{source_labels[summary.source]}",
        f"- 模式：{mode_labels[summary.mode]}",
        f"- 批次：`{summary.batch_id}`",
        f"- 开始：{summary.started_at.astimezone(SHANGHAI_TIMEZONE):%Y-%m-%d %H:%M:%S}",
        f"- 结束：{summary.finished_at.astimezone(SHANGHAI_TIMEZONE):%Y-%m-%d %H:%M:%S}",
        f"- 耗时：{duration_seconds} 秒",
        f"- 总状态：`{summary.status.value}`",
        "",
        "| 任务 | 状态 | 页数 | 条数 | 结果 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    # task_lines 单独累计，超长时只省略后续任务明细。
    task_lines: list[str] = []
    omitted_count = 0
    for task_index, task in enumerate(summary.tasks):
        # 结果列优先显示 CSV 文件名，其次显示稳定错误分类。
        result_text = task.csv_filename or task.error_category or "-"
        task_line = (
            f"| {_escape_markdown_cell(task.display_name)} "
            f"| `{task.status.value}` | {task.saved_pages} | {task.saved_items} "
            f"| {_escape_markdown_cell(result_text)} |"
        )
        # 预留省略提示空间，避免最终追加后超过本地上限。
        candidate_lines = lines + task_lines + [task_line, "", "另有 9999 个任务未展开"]
        candidate_markdown = "\n".join(candidate_lines)
        if len(candidate_markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            omitted_count = len(summary.tasks) - task_index
            break
        task_lines.append(task_line)
    lines.extend(task_lines)
    if omitted_count:
        lines.extend(["", f"另有 {omitted_count} 个任务未展开"])
    markdown = "\n".join(lines)
    # 固定头部理论上远低于上限，最终切片只作为防御性兜底。
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        markdown = markdown.encode("utf-8")[:MAX_MARKDOWN_BYTES].decode(
            "utf-8", errors="ignore"
        )
    return title, markdown


class DingTalkNotifier:
    """Send one signed Markdown request without retries or redirects."""

    def __init__(
        self,
        settings: DingTalkSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Inject deterministic transport and time only for isolated tests."""

        # settings 只保留在 Adapter 生命周期内。
        self.settings = settings
        # transport 允许 MockTransport，生产默认使用真实网络。
        self.transport = transport
        # clock 默认返回带时区当前时间。
        self.clock = clock or (lambda: datetime.now(SHANGHAI_TIMEZONE))

    def send_markdown(self, title: str, markdown: str) -> NotificationDeliveryResult:
        """Attempt one signed delivery and return only a safe classification."""

        if not self.settings.valid:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category=self.settings.error_category
                or "notification_config_invalid",
            )
        # valid 属性保证下列进程内凭证存在。
        webhook_url = self.settings.webhook_url
        secret = self.settings.secret
        if webhook_url is None or secret is None:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_config_invalid",
            )
        # 钉钉签名时间戳使用 UTC epoch 毫秒，与本地展示时区无关。
        timestamp_ms = int(self.clock().timestamp() * 1000)
        signature = create_signature(timestamp_ms, secret)
        # copy_add_param 保留已校验 Webhook 中的 access_token，再追加签名参数。
        signed_url = (
            httpx.URL(webhook_url)
            .copy_add_param("timestamp", timestamp_ms)
            .copy_add_param("sign", signature)
        )
        # Markdown 请求不包含 @ 人配置。
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": markdown},
            "at": {"isAtAll": False, "atMobiles": [], "atUserIds": []},
        }
        # 通知客户端与采集 HTTPX 完全隔离，禁止重定向且不自动重试。
        timeout = httpx.Timeout(connect=5, read=10, write=10, pool=5)
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    signed_url,
                    json=payload,
                )
        except httpx.TimeoutException:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_timeout",
            )
        except httpx.RequestError:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_network_error",
            )
        if not 200 <= response.status_code < 300:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_http_error",
                status_code=response.status_code,
            )
        try:
            # 响应正文只在内存中解析成功码，不保存或写入日志。
            response_payload: Any = response.json()
        except ValueError:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_invalid_response",
                status_code=response.status_code,
            )
        if not isinstance(response_payload, dict) or "errcode" not in response_payload:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_invalid_response",
                status_code=response.status_code,
            )
        if response_payload.get("errcode") != 0:
            return NotificationDeliveryResult(
                status=NotificationDeliveryStatus.FAILED,
                error_category="notification_rejected",
                status_code=response.status_code,
            )
        return NotificationDeliveryResult(
            status=NotificationDeliveryStatus.SUCCEEDED,
            status_code=response.status_code,
        )


def deliver_batch_notification(
    summary: BatchNotificationSummary,
    runtime_logger: RuntimeLogger,
    *,
    transport: httpx.BaseTransport | None = None,
) -> NotificationDeliveryResult:
    """Send one batch summary and emit safe lifecycle events without raising."""

    # 批次级日志上下文不伪造 run_id 或 task_id。
    log_context = LogContext(batch_id=summary.batch_id)
    try:
        # 配置、渲染和网络 Adapter 都收口在通知边界内。
        settings = load_dingtalk_settings()
    except Exception:
        settings = DingTalkSettings(
            enabled=True,
            webhook_url=None,
            secret=None,
            error_category="notification_config_invalid",
        )
    if not settings.enabled and settings.error_category is None:
        result = NotificationDeliveryResult(status=NotificationDeliveryStatus.DISABLED)
        _safe_emit(
            runtime_logger,
            level="INFO",
            event="notification_disabled",
            message="钉钉通知未启用",
            stage="notification",
            context=log_context,
            details={"batch_status": summary.status.value},
        )
        return result
    _safe_emit(
        runtime_logger,
        level="INFO",
        event="notification_pending",
        message="正在发送钉钉批次汇总",
        stage="notification",
        context=log_context,
        details={"batch_status": summary.status.value},
    )
    try:
        # 渲染或 Adapter 的任何未预期错误都只降级为安全分类。
        title, markdown = render_batch_markdown(summary)
        result = DingTalkNotifier(settings, transport=transport).send_markdown(
            title, markdown
        )
    except Exception:
        result = NotificationDeliveryResult(
            status=NotificationDeliveryStatus.FAILED,
            error_category="notification_internal_error",
        )
    if result.status is NotificationDeliveryStatus.SUCCEEDED:
        _safe_emit(
            runtime_logger,
            level="INFO",
            event="notification_succeeded",
            message="钉钉批次汇总发送成功",
            stage="notification",
            context=log_context,
            details={
                "batch_status": summary.status.value,
                "status_code": result.status_code,
            },
        )
    else:
        _safe_emit(
            runtime_logger,
            level="ERROR",
            event="notification_failed",
            message=(
                "钉钉批次汇总发送失败，"
                f"category={result.error_category or 'notification_error'}"
            ),
            stage="notification",
            context=log_context,
            details={
                "batch_status": summary.status.value,
                "status_code": result.status_code,
                "error_category": result.error_category or "notification_error",
            },
        )
    return result


def run_notification_test(
    runtime_logger: RuntimeLogger,
    *,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Send one explicit real-or-mocked test message and return delivery status."""

    # 测试批次 ID 只用于串联安全 JSONL，不包含主机或用户信息。
    test_batch_id = f"notify_test_{uuid4().hex}"
    log_context = LogContext(batch_id=test_batch_id)
    settings = load_dingtalk_settings()
    if not settings.enabled or not settings.valid:
        runtime_logger.emit(
            level="ERROR",
            event="notification_test_failed",
            message="钉钉测试消息未发送，category=notification_config_invalid",
            stage="notification",
            context=log_context,
            details={"error_category": "notification_config_invalid"},
        )
        return 1
    # 测试消息只包含固定标题和北京时间，不暴露运行环境。
    current_time = datetime.now(SHANGHAI_TIMEZONE)
    title = _batch_title(BatchNotificationStatus.SUCCESS, test=True)
    markdown = (
        f"### {title}\n\n"
        f"- 时间：{current_time:%Y-%m-%d %H:%M:%S}\n"
        "- 结果：配置可用"
    )
    result = DingTalkNotifier(settings, transport=transport).send_markdown(title, markdown)
    if result.status is NotificationDeliveryStatus.SUCCEEDED:
        runtime_logger.emit(
            level="INFO",
            event="notification_test_succeeded",
            message="钉钉测试消息发送成功",
            stage="notification",
            context=log_context,
            details={"status_code": result.status_code},
        )
        return 0
    runtime_logger.emit(
        level="ERROR",
        event="notification_test_failed",
        message=(
            "钉钉测试消息发送失败，"
            f"category={result.error_category or 'notification_error'}"
        ),
        stage="notification",
        context=log_context,
        details={
            "status_code": result.status_code,
            "error_category": result.error_category or "notification_error",
        },
    )
    return 1
