"""Stage-three safety logging, diagnostics, and retention tests."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from compass_collector.config import RetentionConfig
from compass_collector.retention import cleanup_runtime
from compass_collector.runtime_logging import LogContext, RuntimeLogger


# 测试时间与工程业务时区一致。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_jsonl_logging_requires_safe_fields_and_task_context(tmp_path: Path) -> None:
    """Write one complete task event and reject an unreviewed detail field."""

    # 独立日志目录避免污染真实 runtime。
    logger = RuntimeLogger(tmp_path / "logs")
    # 三元上下文用于验证每条任务日志可完整定位。
    context = LogContext(
        batch_id="batch-safe",
        task_id="task_safe",
        category_run_id="category-safe",
    )
    logger.emit(
        level="INFO",
        event="page_collected",
        message="安全进度",
        stage="collection",
        context=context,
        details={"page_no": 1, "saved_items": 10},
    )
    # 当天只应产生一个 JSONL 文件。
    log_path = next((tmp_path / "logs").glob("*.jsonl"))
    # 单行日志用于核对结构化上下文。
    payload = json.loads(log_path.read_text(encoding="utf-8"))

    assert payload["batch_id"] == "batch-safe"
    assert payload["category_run_id"] == "category-safe"
    assert "run_id" not in payload
    assert payload["task_id"] == "task_safe"
    assert payload["stage"] == "collection"
    with pytest.raises(ValueError):
        logger.emit(
            level="INFO",
            event="unsafe",
            message="不会落盘",
            stage="test",
            context=context,
            details={"request_headers": "forbidden"},
        )
    with pytest.raises(ValueError):
        logger.emit(
            level="ERROR",
            event="unsafe_message",
            message="sessionid should never be logged",
            stage="test",
            context=context,
        )


def test_retention_deletes_only_expired_disposable_material(tmp_path: Path) -> None:
    """Keep boundary dates and permanent data while deleting older material."""

    # 固定当前时间让三种保留窗口的边界可重复验证。
    current_time = datetime(2026, 7, 16, 15, 0, tzinfo=SHANGHAI_TIMEZONE)
    # 原始响应 30 天窗口分别创建过期和边界日期。
    old_raw = tmp_path / "raw" / "2026-06-16"
    kept_raw = tmp_path / "raw" / "2026-06-17"
    # 失败材料和日志使用 10 天窗口。
    old_artifact = tmp_path / "artifacts" / "2026-07-06"
    kept_artifact = tmp_path / "artifacts" / "2026-07-07"
    old_log = tmp_path / "logs" / "2026-07-06.jsonl"
    kept_log = tmp_path / "logs" / "2026-07-07.jsonl"
    for directory in (old_raw, kept_raw, old_artifact, kept_artifact):
        directory.mkdir(parents=True)
    old_log.parent.mkdir(parents=True)
    old_log.write_text("{}\n", encoding="utf-8")
    kept_log.write_text("{}\n", encoding="utf-8")
    # 永久数据用哨兵文件证明清理函数没有越界。
    database_path = tmp_path / "data" / "collector.db"
    export_path = tmp_path / "exports" / "snapshot.csv"
    profile_path = tmp_path / "browser-profile" / "Preferences"
    for permanent_path in (database_path, export_path, profile_path):
        permanent_path.parent.mkdir(parents=True, exist_ok=True)
        permanent_path.write_text("keep", encoding="utf-8")
    # 保留配置与真实 tasks.yaml 一致。
    config = RetentionConfig(
        raw_response_days=30,
        failure_artifact_days=10,
        log_days=10,
        delete_database_records=False,
        delete_exports=False,
    )

    # 清理摘要只输出数量，不输出被删内容。
    summary = cleanup_runtime(tmp_path, config, now=current_time)

    assert summary.raw_directories == 1
    assert summary.artifact_directories == 1
    assert summary.log_files == 1
    assert not old_raw.exists()
    assert kept_raw.exists()
    assert not old_artifact.exists()
    assert kept_artifact.exists()
    assert not old_log.exists()
    assert kept_log.exists()
    assert database_path.read_text() == "keep"
    assert export_path.read_text() == "keep"
    assert profile_path.read_text() == "keep"
