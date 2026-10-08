"""Strict YAML configuration models for the collector."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compass_collector.app_paths import is_packaged_application, runtime_root
from compass_collector.platforms.compass_config import (
    CategoryTargetConfig as CategoryTargetConfig,
    CategoryScopeConfig,
    FiltersConfig,
    RankConfig,
    DateConfig,
)

from compass_collector.platforms.taobao_config import (
    TaobaoCategoryScopeConfig, TaobaoFiltersConfig, TaobaoRankConfig, TaobaoDateConfig,
)


class StrictModel(BaseModel):
    """Reject unknown configuration fields instead of silently ignoring typos."""

    # 所有配置模型共用严格的未知字段策略。
    model_config = ConfigDict(extra="forbid")


class BrowserSettings(StrictModel):
    """Common visible Chrome settings shared across platforms."""

    channel: Literal["chrome"] = "chrome"
    headless: Literal[False] = False
    locale: str = "zh-CN"
    timezone_id: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    keep_open_after_manual_run: bool = True


class BrowserConfig(BrowserSettings):
    """Resolved session settings, including the selected platform profile."""

    # 每个会话使用已解析的平台 Profile，不共享账号目录。
    profile_dir: Path
    # 仅选定平台可启用已验证的 webdriver 原型兼容方式。
    webdriver_compatibility: bool = False
    # 仅平台显式开启时，在独立 Profile 内补充会话 Cookie 的跨重启恢复。
    persist_session_cookies: bool = False


class PlatformConfig(StrictModel):
    """Configure a platform profile independently from task selection."""

    # 罗盘继续复用旧目录，未来平台必须声明自己的独立目录。
    profile_dir: Path
    # 多平台指定不同数据库连接环境变量，凭证不进入 YAML。
    database_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    # 默认关闭，避免为淘宝排查改变抖音浏览器环境。
    webdriver_compatibility: bool = False
    # 默认关闭，防止改变已有罗盘登录生命周期。
    persist_session_cookies: bool = False
    # 同一平台所有任务共用分类名称黑名单，父级命中时跳过其全部待采子分类。
    category_blacklist: list[str] = Field(default_factory=list)
    # remote 只替换类目范围和黑名单，不改变本机运行配置。
    category_config_source: Literal["yaml", "remote"] = "yaml"

    @field_validator("category_blacklist")
    @classmethod
    def validate_category_blacklist(cls, value: list[str]) -> list[str]:
        """去掉名称首尾空白并去重，拒绝无法辨识的空分类名称。"""

        # 保留首次出现顺序，方便配置快照和人工维护核对。
        names = list(dict.fromkeys(name.strip() for name in value))
        if any(not name for name in names):
            raise ValueError("category_blacklist names must not be blank")
        return names


class IntervalConfig(StrictModel):
    """Configure the randomized delay between serial Compass API requests."""

    # 所有罗盘 API 请求的最小随机间隔，支持风控恢复期的低频运行。
    min: float = Field(ge=0.01, le=10)
    # 所有罗盘 API 请求的最大随机间隔，支持风控恢复期的低频运行。
    max: float = Field(ge=0.01, le=10)

    @field_validator("max")
    @classmethod
    def validate_interval_max(cls, value: float, info) -> float:
        """Require the maximum delay to be no smaller than the minimum."""

        # Pydantic 已校验的最小值用于比较间隔上下界。
        minimum = info.data.get("min")
        if minimum is not None and value < minimum:
            raise ValueError(
                "request interval max must be greater than or equal to min"
            )
        return value


class CollectionConfig(StrictModel):
    """Configure bounded, interruptible serial page operations."""

    # 罗盘页内重试必须先恢复页面状态；淘宝只在首轮后按分类补采。
    network_retry_attempts: int = Field(ge=0, le=3, default=2)
    # 页面操作之间的间隔，沿用已有低频范围。
    request_interval_seconds: IntervalConfig = Field(
        default_factory=lambda: IntervalConfig(min=0.5, max=1)
    )
    # 单次 DOM 操作及完整响应分别有独立超时。
    action_timeout_seconds: float = Field(gt=0, default=30)
    response_timeout_seconds: float = Field(gt=0, default=45)
    # 滚动后等待页面布局稳定，再重新定位分页；等待中仍响应停止。
    scroll_settle_seconds: float = Field(ge=0.1, le=10, default=1.5)
    # 只允许手动任务限时人工恢复，定时任务不等待。
    manual_auth_wait_seconds: float = Field(gt=0, default=180)


class PublicationConfig(StrictModel):
    """Choose the task allowed to update the legacy website index."""

    # 只有主任务可覆盖旧网站根索引，其他任务只更新隔离索引。
    web_primary_task_id: str = "compass_household_cleaning_realtime"


class RetentionConfig(StrictModel):
    """Validate retention values even though cleanup is implemented later."""

    raw_response_days: int = Field(gt=0)
    failure_artifact_days: int = Field(gt=0)
    log_days: int = Field(gt=0)
    delete_database_records: Literal[False] = False
    delete_exports: Literal[False] = False


class DatabaseConfig(StrictModel):
    """Reference a PostgreSQL DSN without storing credentials in configuration."""

    # 连接地址由环境或项目 .env 提供，只支持 PostgreSQL。
    url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")

    @property
    def url(self):
        """Resolve and validate the DSN only when a database operation is requested."""
        from compass_collector.database_connection import database_url

        # 缺少连接时立即失败，不静默回退历史 SQLite。
        value = os.environ.get(self.url_env)
        if not value:
            raise ValueError(f"database environment variable {self.url_env} is required")
        return database_url(value)


class SchedulerConfig(StrictModel):
    """Configure Beijing-time cron execution and delayed-run boundaries."""

    # 首版只支持已经确认的北京时间业务语义。
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    # 误点宽限以分钟配置，默认 10 小时且不允许跨天补实时榜单。
    misfire_grace_minutes: int = Field(gt=0, le=1440)
    cross_day_backfill: Literal[False] = False


class TaskConfig(StrictModel):
    """Describe one independently runnable product ranking task."""

    # 运行时由批次固定远程规则注入；默认不限页，不作为本机规则发布。
    max_pages_per_category: int | None = Field(default=None, gt=0, strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    # 平台名称由显式注册表校验，不允许动态模块路径。
    platform: str = "compass"
    enabled: bool = True
    display_name: str = Field(min_length=1)
    schedule: str = Field(min_length=1)
    rank: RankConfig | TaobaoRankConfig = Field(default_factory=RankConfig)
    # 分类范围每次任务从平台分类树动态发现。
    category_scope: CategoryScopeConfig | TaobaoCategoryScopeConfig
    filters: FiltersConfig | TaobaoFiltersConfig
    date: DateConfig | TaobaoDateConfig

    @model_validator(mode="before")
    @classmethod
    def select_platform_models(cls, value):
        """Parse business fields with the declared platform, never union guessing."""
        if not isinstance(value, dict):
            return value
        # 拷贝配置，不在验证期间改变 YAML 或调用方传入的字典。
        selected = dict(value)
        # 显式选择四个业务类型，阻止抖音和淘宝字段组合被联合模型接受。
        platform = selected.get("platform", "compass")
        if platform not in {"compass", "taobao"}:
            raise ValueError("platform adapter is not registered")
        # 罗盘维持必填的历史契约；淘宝字段可使用已确认的默认条件。
        business_models = (
            {"rank": RankConfig, "category_scope": CategoryScopeConfig, "filters": FiltersConfig, "date": DateConfig}
            if platform == "compass" else
            {"rank": TaobaoRankConfig, "category_scope": TaobaoCategoryScopeConfig, "filters": TaobaoFiltersConfig, "date": TaobaoDateConfig}
        )
        for field_name, business_model in business_models.items():
            if field_name in selected:
                selected[field_name] = business_model.model_validate(selected[field_name])
            elif platform == "taobao":
                selected[field_name] = business_model()
        return selected

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

    browser: BrowserSettings
    # 平台级 Profile 与任务业务配置分离。
    platforms: dict[str, PlatformConfig]
    scheduler: SchedulerConfig
    collection: CollectionConfig
    publication: PublicationConfig = Field(default_factory=PublicationConfig)
    database: DatabaseConfig
    retention: RetentionConfig
    tasks: list[TaskConfig] = Field(min_length=1)

    def for_platform(self, platform: str) -> "AppConfig":
        """Select one platform without enabling disabled tasks or changing its Profile."""
        # 使用真实平台字段筛选，避免任务ID前缀被当作平台归属。
        selected_tasks = [task for task in self.tasks if task.platform == platform]
        if platform not in self.platforms or not selected_tasks:
            raise ValueError("selected platform is not configured")
        # 公开主任务必须仍在所选配置内；淘宝不会更新罗盘兼容索引。
        primary_task_id = self.publication.web_primary_task_id
        if primary_task_id not in {task.id for task in selected_tasks}:
            primary_task_id = selected_tasks[0].id
        return self.model_copy(update={
            "tasks": selected_tasks,
            "platforms": {platform: self.platforms[platform]},
            "database": self.database.model_copy(update={
                "url_env": self.platforms[platform].database_env or self.database.url_env,
            }),
            "publication": self.publication.model_copy(update={"web_primary_task_id": primary_task_id}),
        })

    def execution_platform(self, task_id: str | None = None) -> str:
        """Resolve one task platform or the only enabled platform, never a mixed run."""
        # 显式任务可运行禁用任务，沿用手动任务选择语义。
        candidates = [task for task in self.tasks if task.id == task_id] if task_id else [task for task in self.tasks if task.enabled]
        if task_id and not candidates:
            raise ValueError("selected task is not configured")
        # 单平台没有启用任务时仍允许登录、状态和空闲GUI。
        names = {task.platform for task in candidates} or set(self.platforms)
        if len(names) != 1:
            raise ValueError("multiple platforms require explicit --platform")
        return next(iter(names))

    def browser_for(self, platform: str) -> BrowserConfig:
        """Resolve the selected platform's Chrome configuration."""
        if platform not in self.platforms:
            raise ValueError("platform is not configured")
        return BrowserConfig(
            **self.browser.model_dump(),
            profile_dir=self.platforms[platform].profile_dir,
            webdriver_compatibility=self.platforms[platform].webdriver_compatibility,
            persist_session_cookies=self.platforms[platform].persist_session_cookies,
        )

    @model_validator(mode="after")
    def validate_platforms(self):
        """Reject unsupported platforms and profile aliasing before Chrome starts."""
        # 只接受显式注册的实现；注册并不代表真实平台验收已经完成。
        from compass_collector.platforms.registry import registered_platforms

        if set(self.platforms) - registered_platforms():
            raise ValueError("platform adapter is not registered")
        if any(task.platform not in self.platforms for task in self.tasks):
            raise ValueError("task platform is not configured")
        # resolve 不创建目录；不同平台不能指向同一持久化 Profile。
        profiles = [
            platform.profile_dir.resolve() for platform in self.platforms.values()
        ]
        if len(profiles) != len(set(profiles)):
            raise ValueError("platform profiles must be distinct")
        # 多平台保留独立数据库，环境变量别名不能代替真实归属检查。
        if len(self.platforms) > 1:
            if any(platform.database_env is None for platform in self.platforms.values()):
                raise ValueError("multiple platforms require independent database_env values")
            # 此处只检查变量名；连接后 runtime_platform 再验证真实数据库归属。
            database_names = [platform.database_env for platform in self.platforms.values()]
            if len(database_names) != len(set(database_names)):
                raise ValueError("platform database environment variables must be distinct")
        if self.publication.web_primary_task_id not in {task.id for task in self.tasks}:
            raise ValueError("primary publication task is not configured")
        return self

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

    # 映射后再次校验，避免相对runtime路径与显式便携绝对路径变成同一资源。
    resolved_config = config.model_copy(
        update={
            "platforms": {
                name: platform.model_copy(
                    update={
                        "profile_dir": resolve_runtime_value(platform.profile_dir),
                    }
                )
                for name, platform in config.platforms.items()
            },
        }
    )
    return AppConfig.model_validate(resolved_config.model_dump(mode="python"))
