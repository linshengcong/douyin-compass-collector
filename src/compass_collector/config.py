"""Strict YAML configuration models for the collector."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from compass_collector.app_paths import is_packaged_application, runtime_root


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
    """Configure the randomized delay between serial Compass API requests."""

    # 所有罗盘 API 请求的最小随机间隔。
    min: float = Field(ge=0.01, le=1)
    # 所有罗盘 API 请求的最大随机间隔。
    max: float = Field(ge=0.01, le=1)

    @field_validator("max")
    @classmethod
    def validate_interval_max(cls, value: float, info) -> float:
        """Require the maximum delay to be no smaller than the minimum."""

        # Pydantic 已校验的最小值用于比较间隔上下界。
        minimum = info.data.get("min")
        if minimum is not None and value < minimum:
            raise ValueError("request interval max must be greater than or equal to min")
        return value


class HttpConfig(StrictModel):
    """Configure the synchronous HTTP client shared by category and rank requests."""

    # level1_concurrency 限制同时采集的一级分类组数量。
    level1_concurrency: int = Field(ge=1, le=2, default=1)
    # page_concurrency 限制单个三级分类后续分页的预取 worker 数量。
    page_concurrency: int = Field(ge=1, le=4, default=1)
    # max_in_flight_requests 限制所有分类共享的未完成 HTTP 请求数量。
    max_in_flight_requests: int = Field(ge=1, le=8, default=1)
    # 分类树和榜单分页共用同一请求间隔。
    request_interval_seconds: IntervalConfig
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


class CategoryScopeConfig(StrictModel):
    """Discover target-level descendants below every level-one category."""

    # 当前任务遍历分类接口返回的全部非汇总一级分类。
    mode: Literal["all_level1"] = "all_level1"
    # 当前数据契约只采集三级分类。
    target_level: Literal[3] = 3
    # “全部”节点必须排除，避免与子分类重复。
    exclude_all: Literal[True] = True


class FiltersConfig(StrictModel):
    """Configure the verified product ranking filters."""

    # 当前只开放真实请求验证过的不限和非知名品牌值。
    brand_type: Literal[-1, 0] = -1
    # 当前只开放真实请求验证过的不限和严格大于一万元价格带。
    price_bin: Literal["不限", "10001-?"] = "不限"
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
    """Restrict collection to the verified current-day request semantics."""

    strategy: Literal["today"] = "today"
    date_type: Literal[1] = 1


class TaskConfig(StrictModel):
    """Describe one independently runnable product ranking task."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    enabled: bool = True
    display_name: str = Field(min_length=1)
    schedule: str = Field(min_length=1)
    rank: RankConfig
    # 分类范围每次任务从平台分类树动态发现。
    category_scope: CategoryScopeConfig
    filters: FiltersConfig
    date: DateConfig

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
    config = AppConfig.model_validate(raw_config)
    if not is_packaged_application():
        return config
    # 打包版仍沿用受版本控制的 runtime/... 配置写法，但实际数据存放在应用包内。
    configured_runtime_root = Path("runtime")
    active_runtime_root = runtime_root()

    def resolve_runtime_value(value: Path) -> Path:
        """Map only relative runtime paths into the portable persistent directory."""

        if value.is_absolute() or value.parts[:1] != configured_runtime_root.parts:
            return value
        return active_runtime_root.joinpath(*value.parts[1:])

    return config.model_copy(
        update={
            "browser": config.browser.model_copy(
                update={"profile_dir": resolve_runtime_value(config.browser.profile_dir)}
            ),
            "database": config.database.model_copy(
                update={"path": resolve_runtime_value(config.database.path)}
            ),
        }
    )
