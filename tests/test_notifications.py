"""DingTalk configuration, summary rendering, and isolated transport tests."""

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from compass_collector.notifier import (
    MAX_MARKDOWN_BYTES,
    BatchMode,
    BatchNotificationStatus,
    BatchNotificationSummary,
    BatchSource,
    CategoryNotificationIssue,
    DingTalkNotifier,
    NotificationDeliveryStatus,
    TaskNotificationResult,
    TaskNotificationStatus,
    create_signature,
    deliver_batch_notification,
    load_dingtalk_settings,
    load_project_environment,
    render_batch_markdown,
    run_notification_test,
)
from compass_collector.runtime_logging import RuntimeLogger


# 测试时间与业务时区保持一致。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 假 Webhook 只用于 MockTransport，不会发出真实网络请求。
FAKE_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=fake_test_value"
# 假加签密钥不对应任何真实机器人。
FAKE_SECRET = "fake_signing_value"


def _summary(
    *tasks: TaskNotificationResult,
    mode: BatchMode = BatchMode.OFFICIAL,
) -> BatchNotificationSummary:
    """Build a deterministic notification summary for unit tests."""

    # 固定时间避免渲染断言受当前时钟影响。
    started_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    return BatchNotificationSummary(
        batch_id="batch-test",
        source=BatchSource.TERMINAL,
        mode=mode,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=12),
        tasks=tuple(tasks),
    )


@pytest.fixture(autouse=True)
def clear_dingtalk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent developer machine notification settings from leaking into tests."""

    # 单元测试只使用本文件显式注入的假配置。
    for variable_name in (
        "DINGTALK_ENABLED",
        "DINGTALK_WEBHOOK_URL",
        "DINGTALK_SECRET",
    ):
        monkeypatch.delenv(variable_name, raising=False)


def test_dotenv_loads_missing_values_without_overriding_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give real environment variables priority over repository dotenv values."""

    # 临时 dotenv 不包含任何真实凭证。
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "DINGTALK_ENABLED=true\n"
        "DINGTALK_WEBHOOK_URL=https://example.invalid/from-dotenv\n"
        "DINGTALK_SECRET=dotenv-value\n",
        encoding="utf-8",
    )
    # 外部注入值必须保持最高优先级。
    monkeypatch.setenv("DINGTALK_SECRET", "process-value")

    load_project_environment(dotenv_path)

    assert load_dingtalk_settings().secret is None
    assert os.environ["DINGTALK_SECRET"] == "process-value"


@pytest.mark.parametrize(
    "webhook_url",
    [
        "http://oapi.dingtalk.com/robot/send?access_token=fake",
        "https://example.com/robot/send?access_token=fake",
        "https://oapi.dingtalk.com:443/robot/send?access_token=fake",
        "https://oapi.dingtalk.com/other?access_token=fake",
        "https://oapi.dingtalk.com/robot/send?access_token=fake&extra=1",
        "https://oapi.dingtalk.com/robot/send?access_token=",
        "https://oapi.dingtalk.com/robot/send?access_token=%20",
        "https://oapi.dingtalk.com:bad/robot/send?access_token=fake",
    ],
)
def test_settings_reject_noncanonical_webhooks(
    webhook_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept only the exact signed custom-robot endpoint shape."""

    # 每个非法 URL 都在本地配置边界被拒绝。
    monkeypatch.setenv("DINGTALK_ENABLED", "true")
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", webhook_url)
    monkeypatch.setenv("DINGTALK_SECRET", FAKE_SECRET)

    settings = load_dingtalk_settings()

    assert settings.valid is False
    assert settings.error_category == "notification_config_invalid"


def test_signature_matches_dingtalk_hmac_contract() -> None:
    """Sign timestamp-newline-secret with HMAC-SHA256 and standard Base64."""

    # 固定毫秒时间戳用于独立计算预期签名。
    timestamp_ms = 1784181600000
    # 测试侧重建协议计算，避免自证实现。
    signing_text = f"{timestamp_ms}\n{FAKE_SECRET}".encode("utf-8")
    expected_signature = base64.b64encode(
        hmac.new(
            FAKE_SECRET.encode("utf-8"),
            signing_text,
            hashlib.sha256,
        ).digest()
    ).decode("ascii")

    assert create_signature(timestamp_ms, FAKE_SECRET) == expected_signature


def test_summary_reports_partial_failure_and_never_exposes_csv_path() -> None:
    """Render only approved counters, categories, and the CSV basename."""

    # 一成功一失败应聚合为部分失败。
    summary = _summary(
        TaskNotificationResult(
            task_id="success-task",
            display_name="成功任务",
            status=TaskNotificationStatus.SUCCESS,
            saved_pages=2,
            saved_items=20,
            csv_filename="safe.csv",
        ),
        TaskNotificationResult(
            task_id="failed-task",
            display_name="失败任务",
            status=TaskNotificationStatus.FAILED,
            saved_pages=1,
            saved_items=10,
            error_category="response_contract_error",
            category_issues=(
                CategoryNotificationIssue(
                    category_path="3C数码家电 > 3C数码及配件 > 电脑",
                    error_category="category_unavailable",
                ),
                CategoryNotificationIssue(
                    category_path="智能家居 > 餐饮厨具 > 餐具",
                    error_category="network_error",
                ),
            ),
        ),
    )

    title, markdown = render_batch_markdown(summary)

    assert summary.status is BatchNotificationStatus.PARTIAL_FAILURE
    assert "部分失败" in title
    assert "safe.csv" in markdown
    assert "response_contract_error" in markdown
    assert "耗时：1 分钟" in markdown
    assert "失败 / 跳过分类" in markdown
    assert "电脑 · 已跳过（越权）" in markdown
    assert "餐具 · 失败（network_error）" in markdown
    assert "/Users/" not in markdown
    assert len(markdown.encode("utf-8")) <= MAX_MARKDOWN_BYTES


def test_summary_renders_signed_csv_url_only_as_a_dingtalk_link() -> None:
    """Keep the temporary OSS URL out of local paths while making CSV downloadable."""

    signed_url = "https://oss.example.invalid/exports/safe.csv?signature=fake"
    _, markdown = render_batch_markdown(
        _summary(
            TaskNotificationResult(
                task_id="success-task",
                display_name="成功任务",
                status=TaskNotificationStatus.SUCCESS,
                csv_filename="safe.csv",
                csv_download_url=signed_url,
            )
        )
    )

    assert "[safe.csv](https://oss.example.invalid/exports/safe.csv?signature=fake)" in markdown
    assert "/Users/" not in markdown


def test_summary_marks_oss_upload_failure_without_changing_collection_status() -> None:
    """Show that the CSV stayed published when only its optional OSS upload failed."""

    _, markdown = render_batch_markdown(
        _summary(
            TaskNotificationResult(
                task_id="success-task",
                display_name="成功任务",
                status=TaskNotificationStatus.SUCCESS,
                csv_filename="safe.csv",
                oss_error_category="oss_upload_failed",
            )
        )
    )

    assert "`success`" in markdown
    assert "OSS 上传失败：oss_upload_failed" in markdown


def test_summary_treats_success_plus_idempotent_skip_as_success() -> None:
    """Avoid calling an already-successful skipped task a partial failure."""

    # 手动多任务批次可能同时包含新成功和已成功幂等跳过。
    summary = _summary(
        TaskNotificationResult(
            task_id="new-success",
            display_name="新成功",
            status=TaskNotificationStatus.SUCCESS,
        ),
        TaskNotificationResult(
            task_id="existing-success",
            display_name="已有终态",
            status=TaskNotificationStatus.SKIPPED,
        ),
    )

    assert summary.status is BatchNotificationStatus.SUCCESS


def test_summary_reports_published_partial_success_without_calling_it_failure() -> None:
    """Distinguish one published partial result from a failed task batch."""

    # 单任务已发布 CSV，但内部有一个分类失败。
    summary = _summary(
        TaskNotificationResult(
            task_id="partial-task",
            display_name="部分成功任务",
            status=TaskNotificationStatus.PARTIAL_SUCCESS,
            saved_pages=2,
            saved_items=20,
            csv_filename="partial.csv",
        )
    )

    title, markdown = render_batch_markdown(summary)

    assert summary.status is BatchNotificationStatus.PARTIAL_SUCCESS
    assert "部分成功" in title
    assert "部分失败" not in title
    assert "partial_success" in markdown
    assert "partial.csv" in markdown


def test_dry_run_partial_success_uses_its_own_summary_state() -> None:
    """Label an accepted dry-run partial result without implying publication."""

    # dry-run 同样允许普通分类失败，但不会产生 CSV。
    summary = _summary(
        TaskNotificationResult(
            task_id="dry-partial-task",
            display_name="试运行部分成功",
            status=TaskNotificationStatus.PARTIAL_SUCCESS,
            saved_items=10,
        ),
        mode=BatchMode.DRY_RUN,
    )

    title, _ = render_batch_markdown(summary)

    assert summary.status is BatchNotificationStatus.DRY_RUN_PARTIAL_SUCCESS
    assert "试运行部分成功" in title


def test_partial_success_plus_failed_task_remains_partial_failure() -> None:
    """Use partial_failure only when another top-level task truly failed."""

    # 一个已发布部分成功任务和一个失败任务构成命令级部分失败。
    summary = _summary(
        TaskNotificationResult(
            task_id="partial-task",
            display_name="已发布部分成功",
            status=TaskNotificationStatus.PARTIAL_SUCCESS,
            csv_filename="partial.csv",
        ),
        TaskNotificationResult(
            task_id="failed-task",
            display_name="失败任务",
            status=TaskNotificationStatus.FAILED,
            error_category="network_error",
        ),
    )

    assert summary.status is BatchNotificationStatus.PARTIAL_FAILURE


def test_summary_truncates_task_rows_at_utf8_limit() -> None:
    """Bound the Markdown payload and state how many task rows were omitted."""

    # 长中文任务名用于验证字节上限而不是字符上限。
    tasks = tuple(
        TaskNotificationResult(
            task_id=f"task-{task_index}",
            display_name="超长任务名" * 120,
            status=TaskNotificationStatus.SUCCESS,
        )
        for task_index in range(40)
    )

    _, markdown = render_batch_markdown(_summary(*tasks))

    assert len(markdown.encode("utf-8")) <= MAX_MARKDOWN_BYTES
    assert "个任务未展开" in markdown


def test_notifier_sends_one_signed_markdown_without_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one signed POST with no redirect, retry, or @ recipient."""

    # 固定时钟使 URL 查询签名可确定断言。
    fixed_time = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    # requests 记录 MockTransport 真实接收的唯一请求。
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        """Capture the request and return DingTalk's success contract."""

        requests.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    # 设置值只是 MockTransport 的进程内假凭证。
    monkeypatch.setenv("DINGTALK_ENABLED", "true")
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", FAKE_WEBHOOK)
    monkeypatch.setenv("DINGTALK_SECRET", FAKE_SECRET)
    settings = load_dingtalk_settings()

    result = DingTalkNotifier(
        settings,
        transport=httpx.MockTransport(handle_request),
        clock=lambda: fixed_time,
    ).send_markdown("测试标题", "### 测试")

    assert result.status is NotificationDeliveryStatus.SUCCEEDED
    assert len(requests) == 1
    # 查询值在测试内解析，不输出到日志。
    query = parse_qs(urlparse(str(requests[0].url)).query)
    timestamp_ms = int(fixed_time.timestamp() * 1000)
    assert query["access_token"] == ["fake_test_value"]
    assert query["timestamp"] == [str(timestamp_ms)]
    assert query["sign"] == [create_signature(timestamp_ms, FAKE_SECRET)]
    payload = json.loads(requests[0].content)
    assert payload["msgtype"] == "markdown"
    assert payload["at"] == {"isAtAll": False, "atMobiles": [], "atUserIds": []}


@pytest.mark.parametrize(
    ("response", "expected_category"),
    [
        (httpx.Response(503), "notification_http_error"),
        (httpx.Response(200, text="not-json"), "notification_invalid_response"),
        (
            httpx.Response(200, json={"errcode": 310000, "errmsg": "rejected"}),
            "notification_rejected",
        ),
    ],
)
def test_notifier_classifies_failures_without_response_details(
    response: httpx.Response,
    expected_category: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return stable safe categories for remote failures without retries."""

    # 处理器每次只返回指定的远程结果。
    transport = httpx.MockTransport(lambda request: response)
    monkeypatch.setenv("DINGTALK_ENABLED", "true")
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", FAKE_WEBHOOK)
    monkeypatch.setenv("DINGTALK_SECRET", FAKE_SECRET)

    result = DingTalkNotifier(
        load_dingtalk_settings(),
        transport=transport,
    ).send_markdown("测试", "测试")

    assert result.status is NotificationDeliveryStatus.FAILED
    assert result.error_category == expected_category


def test_delivery_failure_never_raises_or_changes_batch_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep notification delivery as a non-transactional side effect."""

    # MockTransport 主动抛出超时，实现不会重试。
    request_count = 0

    def timeout_request(request: httpx.Request) -> httpx.Response:
        """Raise one deterministic timeout from the notification boundary."""

        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout("mock timeout", request=request)

    monkeypatch.setenv("DINGTALK_ENABLED", "true")
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", FAKE_WEBHOOK)
    monkeypatch.setenv("DINGTALK_SECRET", FAKE_SECRET)
    # log_events 捕获通知边界发出的已脱敏生命周期事件。
    log_events: list[dict] = []
    summary = _summary(
        TaskNotificationResult(
            task_id="task",
            display_name="任务",
            status=TaskNotificationStatus.SUCCESS,
        )
    )

    result = deliver_batch_notification(
        summary,
        RuntimeLogger(tmp_path / "logs", event_sink=log_events.append),
        transport=httpx.MockTransport(timeout_request),
    )

    assert summary.status is BatchNotificationStatus.SUCCESS
    assert result.status is NotificationDeliveryStatus.FAILED
    assert result.error_category == "notification_timeout"
    assert request_count == 1
    assert {event["execution_batch_id"] for event in log_events} == {
        summary.batch_id
    }
    assert {event["batch_id"] for event in log_events} == {None}
    # 持久事件只能包含安全分类，不得落盘假凭证。
    log_text = next((tmp_path / "logs").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "fake_test_value" not in log_text
    assert FAKE_SECRET not in log_text


def test_notify_test_returns_nonzero_when_notification_is_disabled(tmp_path: Path) -> None:
    """Make the explicit connectivity test fail clearly without network access."""

    # 自动清理 fixture 使通知保持默认关闭。
    exit_code = run_notification_test(RuntimeLogger(tmp_path / "logs"))

    assert exit_code == 1
