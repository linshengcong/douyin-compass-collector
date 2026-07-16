"""Conservative date-based cleanup for disposable runtime material."""

import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from compass_collector.config import RetentionConfig


# 保留窗口以北京时间的自然日计算。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """Report only counts and safe relative categories from one cleanup pass."""

    # 已删除原始响应日期目录数量。
    raw_directories: int
    # 已删除失败材料日期目录数量。
    artifact_directories: int
    # 已删除按日 JSONL 文件数量。
    log_files: int
    # 删除失败数量仅用于告警，不包含异常文本或路径。
    failures: int

    def as_log_details(self) -> dict[str, int]:
        """Convert cleanup counts to the single allowlisted logging field."""

        return {
            "raw_directories": self.raw_directories,
            "artifact_directories": self.artifact_directories,
            "log_files": self.log_files,
            "failures": self.failures,
        }


def oldest_retained_date(today: date, retention_days: int) -> date:
    """Keep today plus the preceding retention_days minus one calendar dates."""

    return today - timedelta(days=retention_days - 1)


def parse_date_name(name: str) -> date | None:
    """Parse an exact YYYY-MM-DD name and ignore unrelated runtime entries."""

    try:
        # ISO 日期解析会拒绝非法月份和日期。
        parsed_date = date.fromisoformat(name)
    except ValueError:
        return None
    return parsed_date if parsed_date.isoformat() == name else None


def cleanup_dated_directories(root: Path, cutoff: date) -> tuple[int, int]:
    """Delete only non-symlink date directories strictly older than cutoff."""

    # 删除数量与失败数量独立统计，清理失败不阻断采集。
    deleted_count = 0
    failure_count = 0
    if not root.exists():
        return deleted_count, failure_count
    for candidate in root.iterdir():
        # 非日期目录和符号链接永远不由自动清理处理。
        candidate_date = parse_date_name(candidate.name)
        if (
            candidate_date is None
            or candidate_date >= cutoff
            or not candidate.is_dir()
            or candidate.is_symlink()
        ):
            continue
        try:
            shutil.rmtree(candidate)
            deleted_count += 1
        except OSError:
            failure_count += 1
    return deleted_count, failure_count


def cleanup_log_files(root: Path, cutoff: date) -> tuple[int, int]:
    """Delete only non-symlink YYYY-MM-DD.jsonl files older than cutoff."""

    # 删除数量与失败数量独立统计，避免单文件权限问题阻断采集。
    deleted_count = 0
    failure_count = 0
    if not root.exists():
        return deleted_count, failure_count
    for candidate in root.iterdir():
        # 只有约定扩展名的日期日志参与自动清理。
        candidate_date = parse_date_name(candidate.stem) if candidate.suffix == ".jsonl" else None
        if (
            candidate_date is None
            or candidate_date >= cutoff
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            continue
        try:
            candidate.unlink()
            deleted_count += 1
        except OSError:
            failure_count += 1
    return deleted_count, failure_count


def cleanup_runtime(
    runtime_root: Path,
    config: RetentionConfig,
    *,
    now: datetime | None = None,
) -> CleanupSummary:
    """Apply configured cleanup without touching SQLite, CSV, or Chrome Profile."""

    # 测试可注入固定时间，真实运行使用当前北京时间。
    current_time = now or datetime.now(SHANGHAI_TIMEZONE)
    # 三类材料使用各自独立保留窗口。
    raw_cutoff = oldest_retained_date(current_time.date(), config.raw_response_days)
    artifact_cutoff = oldest_retained_date(
        current_time.date(), config.failure_artifact_days
    )
    log_cutoff = oldest_retained_date(current_time.date(), config.log_days)
    # 原始响应只删除 raw 下的日期一级目录。
    raw_deleted, raw_failures = cleanup_dated_directories(
        runtime_root / "raw", raw_cutoff
    )
    # 失败材料只删除 artifacts 下的日期一级目录。
    artifact_deleted, artifact_failures = cleanup_dated_directories(
        runtime_root / "artifacts", artifact_cutoff
    )
    # 日志只删除 logs 下的按日 JSONL 文件。
    log_deleted, log_failures = cleanup_log_files(runtime_root / "logs", log_cutoff)
    return CleanupSummary(
        raw_directories=raw_deleted,
        artifact_directories=artifact_deleted,
        log_files=log_deleted,
        failures=raw_failures + artifact_failures + log_failures,
    )

