"""Strict YAML configuration models for the collector."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Reject unknown configuration fields instead of silently ignoring typos."""

    # 所有配置模型共用严格的未知字段策略。
    model_config = ConfigDict(extra="forbid")


class BrowserConfig(StrictModel):
    """Configure the persistent Chrome profile used for authentication."""

    channel: Literal["chrome"] = "chrome"
    headless: Literal[False] = False
    profile_dir: Path
    locale: str = "zh-CN"
    timezone_id: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    keep_open_after_manual_run: bool = True


class AuthConfig(StrictModel):
    """Configure the Cookie-name allowlist without ever storing Cookie values."""

    cookie_names: list[str] = Field(min_length=1)

    @field_validator("cookie_names")
    @classmethod
    def validate_cookie_names(cls, value: list[str]) -> list[str]:
        """Require unique, non-empty Cookie names."""

        # 去除名称两端空白，避免配置中出现视觉上难以发现的错误。
        normalized_names = [name.strip() for name in value]
        if any(not name for name in normalized_names):
            raise ValueError("cookie_names cannot contain blank names")
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("cookie_names cannot contain duplicates")
        return normalized_names


class IntervalConfig(StrictModel):
    """Configure the randomized delay between successful page requests."""

    min: float = Field(ge=1, le=2)
    max: float = Field(ge=1, le=2)

    @field_validator("max")
    @classmethod
    def validate_interval_max(cls, value: float, info) -> float:
        """Require the maximum delay to be no smaller than the minimum."""

        # Pydantic 已校验的最小值用于比较间隔上下界。
        minimum = info.data.get("min")
        if minimum is not None and value < minimum:
            raise ValueError("page interval max must be greater than or equal to min")
        return value


class HttpConfig(StrictModel):
    """Configure the synchronous HTTP client used by stage one."""

    concurrency: Literal[1] = 1
    page_interval_seconds: IntervalConfig
    connect_timeout_seconds: float = Field(gt=0)
    read_timeout_seconds: float = Field(gt=0)


class RetentionConfig(StrictModel):
    """Validate retention values even though cleanup is implemented later."""

    raw_response_days: int = Field(gt=0)
    failure_artifact_days: int = Field(gt=0)
    log_days: int = Field(gt=0)
    delete_database_records: Literal[False] = False
    delete_exports: Literal[False] = False


class DatabaseConfig(StrictModel):
    """Configure the local SQLite database managed by Alembic."""

    path: Path


class SchedulerConfig(StrictModel):
    """Configure Beijing-time cron execution and delayed-run boundaries."""

    # 首版只支持已经确认的北京时间业务语义。
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    # 误点宽限以分钟配置，默认 10 小时且不允许跨天补实时榜单。
    misfire_grace_minutes: int = Field(gt=0, le=1440)
    cross_day_backfill: Literal[False] = False


class FilterOption(StrictModel):
    """Pair a platform ID with its human-readable name."""

    id: int = Field(gt=0)
    name: str = Field(min_length=1)


class FiltersConfig(StrictModel):
    """Configure the verified product ranking filters."""

    industry: FilterOption
    category: FilterOption
    brand_type: Literal[-1] = -1
    price_bin: Literal["不限"] = "不限"
    search_info: Literal[""] = ""


class RankConfig(StrictModel):
    """Configure the verified product hot-sale endpoint."""

    type: Literal["product_hot_sale"] = "product_hot_sale"
    endpoint_path: Literal[
        "/compass_api/shop/product/product_rank/market_hot_sale"
    ]
    rank_data_type: Literal[1] = 1
    activity_id: Literal[""] = ""


class DateConfig(StrictModel):
    """Restrict stage one to the verified current-day request semantics."""

    strategy: Literal["today"] = "today"
    date_type: Literal[1] = 1


class PaginationConfig(StrictModel):
    """Configure a page-aligned item limit for the fixed ten-item endpoint."""

    max_items: int = Field(gt=0, le=200, multiple_of=10)


class TaskConfig(StrictModel):
    """Describe one independently runnable product ranking task."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    enabled: bool = True
    display_name: str = Field(min_length=1)
    schedule: str = Field(min_length=1)
    rank: RankConfig
    filters: FiltersConfig
    date: DateConfig
    pagination: PaginationConfig

    @field_validator("schedule")
    @classmethod
    def validate_daily_schedule(cls, value: str) -> str:
        """Restrict v1 Scheduler semantics to one fixed Beijing time per day."""

        # 首版只接受分钟、小时和三个通配符，避免猜测复杂 cron 的业务日期。
        cron_parts = value.split()
        if len(cron_parts) != 5 or cron_parts[2:] != ["*", "*", "*"]:
            raise ValueError("schedule must be '<minute> <hour> * * *'")
        try:
            # 固定分钟和小时必须是十进制整数。
            minute = int(cron_parts[0])
            hour = int(cron_parts[1])
        except ValueError as error:
            raise ValueError("schedule minute and hour must be integers") from error
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError("schedule minute or hour is out of range")
        return value


class AppConfig(StrictModel):
    """Aggregate all currently supported configuration sections."""

    browser: BrowserConfig
    scheduler: SchedulerConfig
    auth: AuthConfig
    http: HttpConfig
    database: DatabaseConfig
    retention: RetentionConfig
    tasks: list[TaskConfig] = Field(min_length=1)

    @field_validator("tasks")
    @classmethod
    def validate_task_ids(cls, value: list[TaskConfig]) -> list[TaskConfig]:
        """Require task IDs to be unique so CLI selection is deterministic."""

        # 任务 ID 列表用于检查重复配置。
        task_ids = [task.id for task in value]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique")
        return value


def load_config(config_path: Path) -> AppConfig:
    """Load YAML and validate every field before any browser is started."""

    # 配置原文只在启动时读取，其中不允许出现 Cookie 值。
    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict):
        raise ValueError("configuration root must be a mapping")
    return AppConfig.model_validate(raw_config)
