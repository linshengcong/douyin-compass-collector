"""SQLite schema, Alembic upgrades, publication identity, and scheduler state."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from compass_collector.errors import PublicationError
from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    CollectedCategoryBatch,
    RawPageRecord,
)


# Scheduler 只把这些非运行态视为已处理的计划执行。
TERMINAL_BATCH_STATUSES = (
    "success",
    "partial_success",
    "failed",
    "auth_required",
    "interrupted",
    "abandoned",
    "missed",
    "skipped_busy",
)


class _StagedCsvExportLike(Protocol):
    """Describe the staged CSV operations coordinated by the database transaction."""

    # temporary_path 和 final_path 均位于 runtime/exports。
    temporary_path: Path
    final_path: Path

    def publish(self) -> None:
        """Atomically move the complete temporary CSV to its final path."""

    def rollback(self) -> None:
        """Remove only files owned by this publication attempt."""


def _rollback_staged_csv_or_raise(staged_csv: _StagedCsvExportLike) -> None:
    """Rollback one staged CSV or surface a stable cleanup failure."""

    try:
        staged_csv.rollback()
    except Exception:
        # 清理异常只暴露稳定分类，避免文件系统原文进入日志或通知。
        raise PublicationError(
            "failed to clean up staged CSV",
            category="publication_cleanup_failed",
        ) from None


class Base(DeclarativeBase):
    """Base metadata shared by ORM models and Alembic migrations."""


class CollectionBatch(Base):
    """Store one top-level task attempt, including dry-run and failed attempts."""

    __tablename__ = "collection_batches"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('normal', 'dry_run', 'force')",
            name="ck_collection_batches_mode",
        ),
        CheckConstraint(
            "status IN ('running', 'publishing', 'success', 'partial_success', "
            "'failed', 'auth_required', 'interrupted', 'abandoned', 'missed', "
            "'skipped_busy')",
            name="ck_collection_batches_status",
        ),
        CheckConstraint(
            "version IS NULL OR version >= 1",
            name="ck_collection_batches_version",
        ),
        CheckConstraint(
            "discovered_category_count >= 0 AND successful_category_count >= 0 "
            "AND failed_category_count >= 0 AND not_started_category_count >= 0 "
            "AND saved_page_count >= 0 AND collected_item_count >= 0",
            name="ck_collection_batches_nonnegative_counts",
        ),
        CheckConstraint(
            "successful_category_count + failed_category_count + "
            "not_started_category_count <= discovered_category_count",
            name="ck_collection_batches_category_counts",
        ),
        CheckConstraint(
            "((status = 'success' AND discovered_category_count > 0 "
            "AND successful_category_count = discovered_category_count "
            "AND failed_category_count = 0 AND not_started_category_count = 0) OR "
            "(status = 'partial_success' AND discovered_category_count > 0 "
            "AND successful_category_count > 0 "
            "AND failed_category_count >= 1 "
            "AND not_started_category_count = 0 "
            "AND successful_category_count + failed_category_count = "
            "discovered_category_count) OR "
            "status NOT IN ('success', 'partial_success'))",
            name="ck_collection_batches_publication_counts",
        ),
        CheckConstraint(
            "((status IN ('running', 'publishing') AND finished_at IS NULL) OR "
            "(status NOT IN ('running', 'publishing') AND finished_at IS NOT NULL))",
            name="ck_collection_batches_lifecycle_time",
        ),
        CheckConstraint(
            "(status NOT IN ('failed', 'auth_required', 'interrupted', 'abandoned', "
            "'missed', 'skipped_busy') OR error_category IS NOT NULL)",
            name="ck_collection_batches_terminal_error",
        ),
        CheckConstraint(
            "((status = 'publishing' AND mode IN ('normal', 'force') "
            "AND version IS NOT NULL AND csv_path IS NOT NULL "
            "AND published_at IS NULL) OR status <> 'publishing')",
            name="ck_collection_batches_publishing",
        ),
        CheckConstraint(
            "((status IN ('success', 'partial_success') AND mode = 'dry_run' "
            "AND version IS NULL AND csv_path IS NULL AND published_at IS NULL) OR "
            "(status IN ('success', 'partial_success') AND mode IN ('normal', 'force') "
            "AND version IS NOT NULL AND csv_path IS NOT NULL "
            "AND published_at IS NOT NULL) OR "
            "status NOT IN ('success', 'partial_success'))",
            name="ck_collection_batches_success_publication",
        ),
        CheckConstraint(
            "(status IN ('publishing', 'success', 'partial_success') OR "
            "(version IS NULL AND csv_path IS NULL AND published_at IS NULL))",
            name="ck_collection_batches_unpublished_states",
        ),
        CheckConstraint(
            "(status NOT IN ('missed', 'skipped_busy') OR "
            "(mode = 'normal' AND manifest_path IS NULL "
            "AND category_tree_raw_path IS NULL "
            "AND discovered_category_count = 0 AND successful_category_count = 0 "
            "AND failed_category_count = 0 AND not_started_category_count = 0 "
            "AND saved_page_count = 0 AND collected_item_count = 0))",
            name="ck_collection_batches_scheduler_only",
        ),
        UniqueConstraint(
            "task_id",
            "planned_at",
            "version",
            name="uq_collection_batch_version",
        ),
    )

    # 批次主键与日志、Manifest 和 raw 目录共用。
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 任务标识使用稳定英文 ID，不受显示名称变化影响。
    task_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    # 业务日期与计划时间都按北京墙上时间入库。
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # mode 只表达 normal/dry_run/force，不记录人工或 Scheduler 来源。
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # status 表达批次生命周期和最终结果。
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 只有正在发布或已正式发布的批次占用版本。
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 实际采集批次保存请求筛选快照；旧批次与 Scheduler-only 记录允许为空。
    brand_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_bin: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 根分类 ID 在当次分类树定位成功后填入。
    root_category_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    root_category_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Manifest、分类树 raw 和 CSV 只保存本地安全路径。
    manifest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    category_tree_raw_path: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )
    csv_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 批次统计用于 status、GUI 和钉钉汇总。
    discovered_category_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    successful_category_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    failed_category_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    not_started_category_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    saved_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collected_item_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    # 批次失败只记录稳定错误分类，不保存异常原文。
    error_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # published_at 是是否正式发布的唯一判定字段。
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )


class CategoryRun(Base):
    """Store one discovered level-three category execution inside a batch."""

    __tablename__ = "category_runs"
    __table_args__ = (
        CheckConstraint(
            "discovery_order >= 1",
            name="ck_category_runs_discovery_order",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'not_started', "
            "'interrupted', 'abandoned')",
            name="ck_category_runs_status",
        ),
        CheckConstraint(
            "(api_total IS NULL OR api_total >= 0) "
            "AND (target_page_count IS NULL OR target_page_count >= 1) "
            "AND saved_page_count >= 0 AND saved_item_count >= 0 "
            "AND (failed_page IS NULL OR failed_page >= 1)",
            name="ck_category_runs_counts",
        ),
        CheckConstraint(
            "((status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND saved_page_count = 0 AND saved_item_count = 0) OR "
            "(status = 'not_started' AND started_at IS NULL AND finished_at IS NOT NULL "
            "AND saved_page_count = 0 AND saved_item_count = 0) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('success', 'failed', 'interrupted', 'abandoned') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL))",
            name="ck_category_runs_lifecycle_time",
        ),
        CheckConstraint(
            "(status <> 'success' OR (api_total IS NOT NULL "
            "AND target_page_count IS NOT NULL "
            "AND saved_page_count = target_page_count "
            "AND saved_item_count = api_total "
            "AND failed_page IS NULL AND error_category IS NULL))",
            name="ck_category_runs_success",
        ),
        CheckConstraint(
            "(status NOT IN ('failed', 'interrupted', 'abandoned') "
            "OR error_category IS NOT NULL)",
            name="ck_category_runs_terminal_error",
        ),
        UniqueConstraint(
            "batch_id",
            "category_id",
            name="uq_category_run_category",
        ),
        UniqueConstraint(
            "batch_id",
            "discovery_order",
            name="uq_category_run_discovery_order",
        ),
    )

    # category_run_id 是分类分页 raw、商品排名和日志的共同边界。
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("collection_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 发现顺序保留分类接口原始顺序。
    discovery_order: Mapped[int] = mapped_column(Integer, nullable=False)
    # 一、二、三级分类快照用于 CSV 和历史审计。
    level1_category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    level1_category_name: Mapped[str] = mapped_column(String(512), nullable=False)
    level2_category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    level2_category_name: Mapped[str] = mapped_column(String(512), nullable=False)
    category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category_name: Mapped[str] = mapped_column(String(512), nullable=False)
    # 分类状态独立于批次状态，可表达失败和未开始。
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 分页统计保留平台 total、目标页和已落盘数量。
    api_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saved_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saved_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 失败页与错误分类只在异常终态填写。
    failed_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # not_started 没有 started_at，但使用 finished_at 记录被终止时间。
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RawResponse(Base):
    """Index one validated gzip ranking page for a category run."""

    __tablename__ = "raw_responses"
    __table_args__ = (
        CheckConstraint("page_no >= 1", name="ck_raw_responses_page_no"),
        CheckConstraint("item_count >= 0", name="ck_raw_responses_item_count"),
        UniqueConstraint(
            "category_run_id",
            "page_no",
            name="uq_raw_response_page",
        ),
    )

    # 自增主键只用于 SQLite 内部索引。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("category_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 每一页保存页码、安全路径、条数和实际采集时间。
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProductRankEntryModel(Base):
    """Persist one official product ranking row inside one category run."""

    __tablename__ = "product_rank_entries"
    __table_args__ = (
        CheckConstraint("page_no >= 1", name="ck_product_rank_entries_page_no"),
        CheckConstraint("rank >= 1", name="ck_product_rank_entries_rank"),
        CheckConstraint(
            "pay_amount_min_value >= 0 AND pay_amount_max_value >= 0 "
            "AND pay_amount_min_value <= pay_amount_max_value",
            name="ck_product_rank_entries_pay_amount",
        ),
        CheckConstraint(
            "pay_combo_count_min_value >= 0 AND pay_combo_count_max_value >= 0 "
            "AND pay_combo_count_min_value <= pay_combo_count_max_value",
            name="ck_product_rank_entries_pay_combo_count",
        ),
        UniqueConstraint(
            "category_run_id",
            "product_id",
            name="uq_category_run_product",
        ),
        UniqueConstraint(
            "category_run_id",
            "rank",
            name="uq_category_run_rank",
        ),
    )

    # 商品排名通过 category_run_id 间接归属批次。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("category_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 页码、采集时间和分类内排名保留源数据语义。
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_name: Mapped[str] = mapped_column(String(2048), nullable=False)
    # 图片 URL 允许为空，保证已存在的历史排名可平滑迁移。
    image_url: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    newly_on_ranking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 金额和成交件数保留平台原始区间与单位。
    pay_amount_min_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_amount_max_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_amount_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    pay_combo_count_min_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_combo_count_max_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_combo_count_unit: Mapped[str] = mapped_column(String(32), nullable=False)


class ProductRankEntryShopModel(Base):
    """Persist every shop linked to one product ranking entry in source order."""

    __tablename__ = "product_rank_entry_shops"
    __table_args__ = (
        CheckConstraint(
            "position >= 0",
            name="ck_product_rank_entry_shops_position",
        ),
        UniqueConstraint("entry_id", "position", name="uq_entry_shop_position"),
    )

    # 店铺关系通过 entry_id 跟随商品排名级联删除。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("product_rank_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shop_name: Mapped[str] = mapped_column(String(1024), nullable=False)


class SchedulerCheckpoint(Base):
    """Persist the last reconciliation boundary for one configured task."""

    __tablename__ = "scheduler_checkpoints"

    # 任务 ID 是调度检查点的稳定主键。
    task_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # SQLite 保存北京无时区墙上时间。
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


@dataclass(frozen=True, slots=True)
class PublishedBatch:
    """Expose one officially published batch to idempotence callers."""

    # 只暴露跳过重复执行所需的安全字段。
    batch_id: str
    task_id: str
    planned_at: datetime
    version: int
    csv_path: Path


@dataclass(frozen=True, slots=True)
class PublishedCollectionResult:
    """Return both idempotence metadata and the final Manifest projection."""

    # published_batch 供 runner 日志、通知和后续幂等判断使用。
    published_batch: PublishedBatch
    # snapshot 直接交给 BatchStorage 一次性同步最终 Manifest。
    snapshot: "BatchCollectionSnapshot"


@dataclass(frozen=True, slots=True)
class StatusRow:
    """Represent one concise batch-level status row."""

    # status 在阶段五将扩展为批次摘要与分类明细两级展示。
    batch_id: str
    task_id: str
    planned_at: datetime
    mode: str
    status: str
    version: int | None
    # 筛选快照用于状态页和后续本地网站解释历史批次。
    brand_type: int | None
    price_bin: str | None
    started_at: datetime
    finished_at: datetime | None
    published_at: datetime | None
    error_category: str | None
    csv_path: str | None
    discovered_category_count: int
    successful_category_count: int
    failed_category_count: int
    not_started_category_count: int
    saved_page_count: int
    collected_item_count: int


@dataclass(frozen=True, slots=True)
class CategoryRunSnapshot:
    """Expose one category execution state without leaking ORM instances."""

    # 分类运行 ID 连接 raw、日志和后续正式商品记录。
    category_run_id: str
    batch_id: str
    discovery_order: int
    # 完整分类路径用于 Manifest、CSV 和故障诊断。
    level1_category_id: str
    level1_category_name: str
    level2_category_id: str
    level2_category_name: str
    category_id: str
    category_name: str
    # 分类生命周期和分页统计均来自同一 SQLite 事务。
    status: str
    api_total: int | None
    target_page_count: int | None
    saved_page_count: int
    saved_item_count: int
    failed_page: int | None
    error_category: str | None
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class BatchCollectionSnapshot:
    """Represent the authoritative batch and all ordered category states."""

    # 批次身份字段必须与 BatchStorage 初始化信息完全一致。
    batch_id: str
    task_id: str
    business_date: date
    planned_at: datetime
    mode: str
    status: str
    version: int | None
    # 实际请求筛选值随批次固定，配置修改不会改写历史语义。
    brand_type: int | None
    price_bin: str | None
    # 根分类和安全路径由 SQLite 作为权威来源。
    root_category_id: str | None
    root_category_name: str | None
    manifest_path: str | None
    category_tree_raw_path: str | None
    csv_path: str | None
    # 批次统计通过分类运行统一重算，避免调用方自行拼接。
    discovered_category_count: int
    successful_category_count: int
    failed_category_count: int
    not_started_category_count: int
    saved_page_count: int
    collected_item_count: int
    error_category: str | None
    started_at: datetime
    finished_at: datetime | None
    published_at: datetime | None
    # categories 保持 discovery_order 顺序，供 Manifest 一次性投影。
    categories: tuple[CategoryRunSnapshot, ...]


def normalize_datetime(value: datetime) -> datetime:
    """Store Beijing wall-clock datetimes consistently in SQLite."""

    # SQLite 不保存时区偏移，入库前转为无时区墙上时间。
    return value.replace(tzinfo=None)


def database_url(database_path: Path) -> str:
    """Build an absolute SQLite URL for SQLAlchemy and Alembic."""

    # 绝对路径避免 Alembic 和 CLI 从不同目录启动时指向不同数据库。
    absolute_path = database_path.resolve()
    return f"sqlite:///{absolute_path}"


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Enable SQLite foreign-key enforcement for every new connection."""

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        """Enable foreign keys on one DB-API connection."""

        # DB-API cursor 只执行固定 PRAGMA，不包含用户数据。
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def upgrade_database(database_path: Path) -> None:
    """Upgrade the configured SQLite database to the latest Alembic revision."""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    # 迁移资源在开发时来自仓库，PyInstaller 中来自打包的数据目录。
    from compass_collector.app_paths import resource_root

    project_root = resource_root()
    # Alembic 配置在运行时覆盖为当前数据库绝对 URL。
    alembic_config = AlembicConfig(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url(database_path))
    command.upgrade(alembic_config, "head")


class Database:
    """Provide clean-baseline idempotence, status, and scheduler operations."""

    def __init__(self, database_path: Path) -> None:
        """Create a SQLite engine and session factory for the configured path."""

        database_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLAlchemy Engine 使用与 Alembic 完全一致的绝对 URL。
        self.engine = create_engine(database_url(database_path), future=True)
        enable_sqlite_foreign_keys(self.engine)
        # 事务 Session 禁止提交后自动过期，便于返回摘要。
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            future=True,
        )

    def close(self) -> None:
        """Dispose pooled SQLite connections."""

        self.engine.dispose()

    @staticmethod
    def _category_run_snapshot(category_run: CategoryRun) -> CategoryRunSnapshot:
        """Detach one category row from its transaction as an immutable snapshot."""

        return CategoryRunSnapshot(
            category_run_id=category_run.id,
            batch_id=category_run.batch_id,
            discovery_order=category_run.discovery_order,
            level1_category_id=category_run.level1_category_id,
            level1_category_name=category_run.level1_category_name,
            level2_category_id=category_run.level2_category_id,
            level2_category_name=category_run.level2_category_name,
            category_id=category_run.category_id,
            category_name=category_run.category_name,
            status=category_run.status,
            api_total=category_run.api_total,
            target_page_count=category_run.target_page_count,
            saved_page_count=category_run.saved_page_count,
            saved_item_count=category_run.saved_item_count,
            failed_page=category_run.failed_page,
            error_category=category_run.error_category,
            started_at=category_run.started_at,
            finished_at=category_run.finished_at,
        )

    def _collection_snapshot_from_session(
        self,
        session: Session,
        batch: CollectionBatch,
    ) -> BatchCollectionSnapshot:
        """Build one ordered authoritative snapshot inside the active transaction."""

        # flush 保证刚完成的状态和计数进入随后读取的快照。
        session.flush()
        # 分类必须按发现顺序投影，主键顺序不具备业务含义。
        category_runs = session.scalars(
            select(CategoryRun)
            .where(CategoryRun.batch_id == batch.id)
            .order_by(CategoryRun.discovery_order)
        ).all()
        # ORM 行在事务结束前转换为不可变记录，调用方无需持有 Session。
        category_snapshots = tuple(
            self._category_run_snapshot(category_run)
            for category_run in category_runs
        )
        return BatchCollectionSnapshot(
            batch_id=batch.id,
            task_id=batch.task_id,
            business_date=batch.business_date,
            planned_at=batch.planned_at,
            mode=batch.mode,
            status=batch.status,
            version=batch.version,
            brand_type=batch.brand_type,
            price_bin=batch.price_bin,
            root_category_id=batch.root_category_id,
            root_category_name=batch.root_category_name,
            manifest_path=batch.manifest_path,
            category_tree_raw_path=batch.category_tree_raw_path,
            csv_path=batch.csv_path,
            discovered_category_count=batch.discovered_category_count,
            successful_category_count=batch.successful_category_count,
            failed_category_count=batch.failed_category_count,
            not_started_category_count=batch.not_started_category_count,
            saved_page_count=batch.saved_page_count,
            collected_item_count=batch.collected_item_count,
            error_category=batch.error_category,
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            published_at=batch.published_at,
            categories=category_snapshots,
        )

    def _recalculate_batch_counts(
        self,
        session: Session,
        batch: CollectionBatch,
    ) -> None:
        """Recalculate all collection counters from category-run durable state."""

        # flush 后读取同一批次全部分类，避免使用调用方传入的增量计数。
        session.flush()
        # 分类运行是批次统计的唯一来源，也包含失败前已保存的分页。
        category_runs = session.scalars(
            select(CategoryRun).where(CategoryRun.batch_id == batch.id)
        ).all()
        if len(category_runs) != batch.discovered_category_count:
            raise RuntimeError("category run count does not match batch discovery count")
        # 成功、失败和未开始数量按当前终态统一重算。
        batch.successful_category_count = sum(
            category_run.status == "success" for category_run in category_runs
        )
        batch.failed_category_count = sum(
            category_run.status == "failed" for category_run in category_runs
        )
        batch.not_started_category_count = sum(
            category_run.status == "not_started" for category_run in category_runs
        )
        # 页数和条数包含后来失败分类已经成功登记的 raw 分页。
        batch.saved_page_count = sum(
            category_run.saved_page_count for category_run in category_runs
        )
        batch.collected_item_count = sum(
            category_run.saved_item_count for category_run in category_runs
        )

    def _running_category_context(
        self,
        session: Session,
        category_run_id: str,
    ) -> tuple[CollectionBatch, CategoryRun]:
        """Load one running category and its still-running parent batch."""

        # 分类运行 ID 是分页编排层持有的稳定身份。
        category_run = session.get(CategoryRun, category_run_id)
        if category_run is None:
            raise RuntimeError("category run does not exist")
        # 父批次必须存在且仍处于采集期。
        batch = session.get(CollectionBatch, category_run.batch_id)
        if batch is None:
            raise RuntimeError("collection batch does not exist")
        if batch.status != "running":
            raise RuntimeError("collection batch is not running")
        if category_run.status != "running":
            raise RuntimeError("category run is not running")
        return batch, category_run

    def _validate_collected_batch(
        self,
        session: Session,
        collected_batch: CollectedCategoryBatch,
    ) -> tuple[CollectionBatch, tuple[CategoryRun, ...], str]:
        """Validate one in-memory result against every authoritative category state."""

        # batch_id 是阶段二到阶段四贯穿文件和数据库的稳定身份。
        batch = session.get(CollectionBatch, collected_batch.batch_id)
        if batch is None:
            raise PublicationError(
                "collection batch does not exist",
                category="publication_contract_error",
            )
        if batch.status != "running":
            raise PublicationError(
                "collection batch is not ready for publication",
                category="publication_contract_error",
            )
        if batch.task_id != collected_batch.task_id:
            raise PublicationError(
                "collected task does not match the batch",
                category="publication_contract_error",
            )
        if batch.business_date != collected_batch.business_date:
            raise PublicationError(
                "collected business date does not match the batch",
                category="publication_contract_error",
            )
        if batch.started_at != normalize_datetime(collected_batch.started_at):
            raise PublicationError(
                "collected start time does not match the batch",
                category="publication_contract_error",
            )
        # 批次统计先从 category_runs 重算，调用方计数不能成为权威来源。
        self._recalculate_batch_counts(session, batch)
        # 发布校验按 discovery_order 读取全部分类终态。
        category_runs = tuple(
            session.scalars(
                select(CategoryRun)
                .where(CategoryRun.batch_id == batch.id)
                .order_by(CategoryRun.discovery_order)
            ).all()
        )
        # 正常完成的阶段三批次只能包含 success 与允许发布的分类级 failed。
        if any(
            category_run.status not in {"success", "failed"}
            for category_run in category_runs
        ):
            raise PublicationError(
                "collection batch still has unfinished categories",
                category="publication_contract_error",
            )
        # 成功和失败分类分别用于推导最终批次状态。
        successful_category_runs = tuple(
            category_run
            for category_run in category_runs
            if category_run.status == "success"
        )
        failed_category_runs = tuple(
            category_run
            for category_run in category_runs
            if category_run.status == "failed"
        )
        if not successful_category_runs:
            raise PublicationError(
                "collection batch has no successful category",
                category="publication_contract_error",
            )
        if len(category_runs) != batch.discovered_category_count:
            raise PublicationError(
                "collection batch category count is inconsistent",
                category="publication_contract_error",
            )
        if collected_batch.failed_category_count != len(failed_category_runs):
            raise PublicationError(
                "collected failure count does not match SQLite",
                category="publication_contract_error",
            )
        # 内存结果必须按发现顺序包含全部且仅包含 success 分类。
        collected_category_run_ids = tuple(
            collected_category_run.plan.category_run_id
            for collected_category_run in collected_batch.category_runs
        )
        successful_category_run_ids = tuple(
            category_run.id for category_run in successful_category_runs
        )
        if collected_category_run_ids != successful_category_run_ids:
            raise PublicationError(
                "collected categories do not match all successful categories",
                category="publication_contract_error",
            )
        for collected_category_run, category_run in zip(
            collected_batch.category_runs,
            successful_category_runs,
            strict=True,
        ):
            # 分类路径快照不允许在采集完成后被调用方改写。
            collected_category = collected_category_run.plan.category
            collected_category_signature = (
                collected_category.discovery_order,
                collected_category.level1_category_id,
                collected_category.level1_category_name,
                collected_category.level2_category_id,
                collected_category.level2_category_name,
                collected_category.category_id,
                collected_category.category_name,
            )
            persisted_category_signature = (
                category_run.discovery_order,
                category_run.level1_category_id,
                category_run.level1_category_name,
                category_run.level2_category_id,
                category_run.level2_category_name,
                category_run.category_id,
                category_run.category_name,
            )
            if collected_category_signature != persisted_category_signature:
                raise PublicationError(
                    "collected category identity changed before publication",
                    category="publication_contract_error",
                )
            if (
                collected_category_run.api_total != category_run.api_total
                or collected_category_run.target_page_count
                != category_run.target_page_count
                or len(collected_category_run.raw_pages)
                != category_run.saved_page_count
                or len(collected_category_run.entries)
                != category_run.saved_item_count
            ):
                raise PublicationError(
                    "collected category totals do not match SQLite",
                    category="publication_contract_error",
                )
            if (
                normalize_datetime(collected_category_run.started_at)
                != category_run.started_at
                or normalize_datetime(collected_category_run.finished_at)
                != category_run.finished_at
            ):
                raise PublicationError(
                    "collected category lifecycle does not match SQLite",
                    category="publication_contract_error",
                )
            # raw_pages 必须覆盖连续目标页并累计到分类 total。
            collected_page_numbers = tuple(
                raw_page.page_no for raw_page in collected_category_run.raw_pages
            )
            expected_page_numbers = tuple(
                range(1, category_run.saved_page_count + 1)
            )
            collected_item_count = sum(
                raw_page.item_count for raw_page in collected_category_run.raw_pages
            )
            if (
                collected_page_numbers != expected_page_numbers
                or collected_item_count != category_run.saved_item_count
            ):
                raise PublicationError(
                    "collected raw pages do not match SQLite progress",
                    category="publication_contract_error",
                )
        # 零失败为 success；至少一个成功与任意数量分类失败为 partial_success。
        final_status = "success" if not failed_category_runs else "partial_success"
        return batch, category_runs, final_status

    @staticmethod
    def _ensure_no_product_entries(
        session: Session,
        category_runs: tuple[CategoryRun, ...],
    ) -> None:
        """Reject a dirty publication attempt that already owns official rows."""

        # 所有分类运行 ID 共同限定本批次的正式商品记录范围。
        category_run_ids = tuple(category_run.id for category_run in category_runs)
        # 调用方保证至少一个成功分类，因此 ID 集合不会为空。
        existing_entry_count = session.scalar(
            select(func.count())
            .select_from(ProductRankEntryModel)
            .where(ProductRankEntryModel.category_run_id.in_(category_run_ids))
        )
        if (existing_entry_count or 0) != 0:
            raise PublicationError(
                "collection batch already contains product entries",
                category="publication_contract_error",
            )

    @staticmethod
    def _insert_product_entries(
        session: Session,
        collected_batch: CollectedCategoryBatch,
    ) -> None:
        """Insert every successful category product and its ordered shops."""

        for collected_category_run in collected_batch.category_runs:
            # category_run_id 让相同商品或排名可合法出现在不同分类。
            category_run_id = collected_category_run.plan.category_run_id
            for entry in collected_category_run.entries:
                # 商品主记录先 flush 获取 shop 外键所需的自增 ID。
                entry_model = ProductRankEntryModel(
                    category_run_id=category_run_id,
                    captured_at=normalize_datetime(entry.captured_at),
                    page_no=entry.page_no,
                    rank=entry.rank,
                    product_id=entry.product_id,
                    product_name=entry.product_name,
                    image_url=entry.image_url,
                    newly_on_ranking=entry.newly_on_ranking,
                    pay_amount_min_value=entry.pay_amount.min_value,
                    pay_amount_max_value=entry.pay_amount.max_value,
                    pay_amount_unit=entry.pay_amount.unit,
                    pay_combo_count_min_value=entry.pay_combo_count.min_value,
                    pay_combo_count_max_value=entry.pay_combo_count.max_value,
                    pay_combo_count_unit=entry.pay_combo_count.unit,
                )
                session.add(entry_model)
                session.flush()
                for shop in entry.shops:
                    # 店铺 position 保持接口原始顺序并受唯一约束保护。
                    session.add(
                        ProductRankEntryShopModel(
                            entry_id=entry_model.id,
                            position=shop.position,
                            shop_id=shop.shop_id,
                            shop_name=shop.shop_name,
                        )
                    )

    def collection_snapshot(self, batch_id: str) -> BatchCollectionSnapshot:
        """Read the current SQLite state for retrying a failed Manifest sync."""

        with self.session_factory() as session:
            # 主键读取确保快照只属于调用方指定批次。
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise RuntimeError("collection batch does not exist")
            # 返回值在 Session 关闭前已经完全解耦 ORM。
            snapshot = self._collection_snapshot_from_session(session, batch)
        return snapshot

    def create_batch(
        self,
        *,
        batch_id: str,
        task_id: str,
        business_date: date,
        planned_at: datetime,
        mode: str,
        brand_type: int,
        price_bin: str,
        manifest_path: Path,
        started_at: datetime,
    ) -> None:
        """Create one running task batch before the category-tree request."""

        # SQLite 统一保存北京无时区墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        # 批次开始时间与计划时间使用相同的存储规则。
        stored_started_at = normalize_datetime(started_at)
        with self.session_factory.begin() as session:
            # batch_id 由编排层提前生成，重复创建必须显式失败。
            if session.get(CollectionBatch, batch_id) is not None:
                raise RuntimeError("collection batch already exists")
            session.add(
                CollectionBatch(
                    id=batch_id,
                    task_id=task_id,
                    business_date=business_date,
                    planned_at=stored_planned_at,
                    mode=mode,
                    status="running",
                    version=None,
                    brand_type=brand_type,
                    price_bin=price_bin,
                    root_category_id=None,
                    root_category_name=None,
                    manifest_path=str(manifest_path),
                    category_tree_raw_path=None,
                    csv_path=None,
                    discovered_category_count=0,
                    successful_category_count=0,
                    failed_category_count=0,
                    not_started_category_count=0,
                    saved_page_count=0,
                    collected_item_count=0,
                    error_category=None,
                    started_at=stored_started_at,
                    finished_at=None,
                    published_at=None,
                )
            )

    def record_category_tree_raw(
        self,
        *,
        batch_id: str,
        category_tree_raw_path: Path,
    ) -> None:
        """Attach the saved category-tree response to one running batch."""

        if not category_tree_raw_path.is_file():
            raise FileNotFoundError(category_tree_raw_path)
        with self.session_factory.begin() as session:
            # 主键读取保证分类树只能关联到目标批次。
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise RuntimeError("collection batch does not exist")
            if batch.status != "running":
                raise RuntimeError("collection batch is not running")
            if batch.category_tree_raw_path is not None:
                raise RuntimeError("category tree has already been recorded")
            # 数据库只保存本地路径，不保存响应正文。
            batch.category_tree_raw_path = str(category_tree_raw_path)

    def create_category_runs(
        self,
        *,
        batch_id: str,
        discovery: CategoryDiscoveryResult,
        category_run_plans: tuple[CategoryRunPlan, ...],
    ) -> None:
        """Create every pending category run and update its batch atomically."""

        if not discovery.categories:
            raise ValueError("category discovery result cannot be empty")
        # 计划数量必须与解析结果严格一致，避免遗漏或额外分类。
        if len(category_run_plans) != len(discovery.categories):
            raise ValueError("category run plans do not match discovery result")
        # 每个计划必须保留解析结果的原始顺序和对象值。
        planned_categories = tuple(plan.category for plan in category_run_plans)
        if planned_categories != discovery.categories:
            raise ValueError("category run plan order does not match discovery result")
        # category_run_id 在事务前检查唯一性，数据库主键继续作为最终防线。
        category_run_ids = [plan.category_run_id for plan in category_run_plans]
        if len(category_run_ids) != len(set(category_run_ids)):
            raise ValueError("category run ids must be unique")

        with self.session_factory.begin() as session:
            # 分类批量创建前锁定批次生命周期和一次性调用语义。
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise RuntimeError("collection batch does not exist")
            if batch.status != "running":
                raise RuntimeError("collection batch is not running")
            if batch.category_tree_raw_path is None:
                raise RuntimeError("category tree must be recorded before categories")
            # 已存在任意分类表示本批次已经完成过发现登记。
            existing_category_run_id = session.scalar(
                select(CategoryRun.id)
                .where(CategoryRun.batch_id == batch_id)
                .limit(1)
            )
            if existing_category_run_id is not None:
                raise RuntimeError("category runs have already been created")
            # 批次根分类和发现数量与分类运行在同一事务内生效。
            batch.root_category_id = discovery.root_category_id
            batch.root_category_name = discovery.root_category_name
            batch.discovered_category_count = len(discovery.categories)
            for plan in category_run_plans:
                # 分类快照来自当次真实分类树，不依赖后续名称变化。
                category = plan.category
                session.add(
                    CategoryRun(
                        id=plan.category_run_id,
                        batch_id=batch_id,
                        discovery_order=category.discovery_order,
                        level1_category_id=category.level1_category_id,
                        level1_category_name=category.level1_category_name,
                        level2_category_id=category.level2_category_id,
                        level2_category_name=category.level2_category_name,
                        category_id=category.category_id,
                        category_name=category.category_name,
                        status="pending",
                        api_total=None,
                        target_page_count=None,
                        saved_page_count=0,
                        saved_item_count=0,
                        failed_page=None,
                        error_category=None,
                        started_at=None,
                        finished_at=None,
                    )
                )

    def finish_discovery_failure(
        self,
        *,
        batch_id: str,
        status: str,
        error_category: str,
        finished_at: datetime,
        root_category_id: str | None = None,
        root_category_name: str | None = None,
    ) -> None:
        """Finish a failed discovery and close any already-created pending runs."""

        # 分类发现阶段只允许写入三个明确的失败终态。
        allowed_statuses = {"failed", "auth_required", "interrupted"}
        if status not in allowed_statuses:
            raise ValueError("unsupported discovery failure status")
        if not error_category:
            raise ValueError("discovery failure requires an error category")
        if (root_category_id is None) != (root_category_name is None):
            raise ValueError("root category id and name must be provided together")
        # 失败时间按 SQLite 墙上时间保存。
        stored_finished_at = normalize_datetime(finished_at)
        with self.session_factory.begin() as session:
            # 同一批次只能从 running 进入发现失败终态。
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise RuntimeError("collection batch does not exist")
            if batch.status != "running":
                raise RuntimeError("collection batch is not running")
            # 极端情况下已登记但尚未开始的分类统一标记 not_started。
            pending_category_runs = session.scalars(
                select(CategoryRun).where(
                    CategoryRun.batch_id == batch_id,
                    CategoryRun.status == "pending",
                )
            ).all()
            for category_run in pending_category_runs:
                category_run.status = "not_started"
                category_run.finished_at = stored_finished_at
            # 根分类可在解析后、Manifest 更新前失败时保留安全快照。
            if root_category_id is not None:
                if batch.root_category_id not in {None, root_category_id}:
                    raise ValueError("root category id conflicts with the batch")
                batch.root_category_id = root_category_id
            if root_category_name is not None:
                if batch.root_category_name not in {None, root_category_name}:
                    raise ValueError("root category name conflicts with the batch")
                batch.root_category_name = root_category_name
            batch.not_started_category_count = len(pending_category_runs)
            batch.status = status
            batch.error_category = error_category
            batch.finished_at = stored_finished_at

    def start_category_run(
        self,
        category_run_id: str,
        started_at: datetime,
    ) -> BatchCollectionSnapshot:
        """Start one pending category while the batch remains in collection state."""

        # SQLite 保存无时区的北京时间墙上时间。
        stored_started_at = normalize_datetime(started_at)
        with self.session_factory.begin() as session:
            # 分类必须存在且仍处于尚未开始状态。
            category_run = session.get(CategoryRun, category_run_id)
            if category_run is None:
                raise RuntimeError("category run does not exist")
            # 父批次必须仍处于采集期。
            batch = session.get(CollectionBatch, category_run.batch_id)
            if batch is None:
                raise RuntimeError("collection batch does not exist")
            if batch.status != "running":
                raise RuntimeError("collection batch is not running")
            if category_run.status != "pending":
                raise RuntimeError("category run is not pending")
            # 一级分类并发时允许多个 running 分类；调度层保证每个一级组内顺序。
            # 状态与开始时间在同一事务中生效。
            category_run.status = "running"
            category_run.started_at = stored_started_at
            self._recalculate_batch_counts(session, batch)
            # Manifest 将直接消费该权威快照，不自行推导增量。
            snapshot = self._collection_snapshot_from_session(session, batch)
        return snapshot

    def record_category_page(
        self,
        category_run_id: str,
        raw_page: RawPageRecord,
        api_total: int,
        target_page_count: int,
    ) -> BatchCollectionSnapshot:
        """Index one existing raw page and advance category progress atomically."""

        if not raw_page.path.is_file():
            raise FileNotFoundError(raw_page.path)
        if raw_page.page_no < 1:
            raise ValueError("page number must be positive")
        if raw_page.item_count < 0:
            raise ValueError("page item count cannot be negative")
        if api_total < 0:
            raise ValueError("api total cannot be negative")
        if target_page_count < 1:
            raise ValueError("target page count must be positive")
        # 页面采集时间使用与其他运行时间相同的 SQLite 规则。
        stored_captured_at = normalize_datetime(raw_page.captured_at)
        with self.session_factory.begin() as session:
            # 公共 helper 同时校验分类和父批次仍为 running。
            batch, category_run = self._running_category_context(
                session,
                category_run_id,
            )
            # 页码必须从一开始连续递增，拒绝重复页和跳页。
            expected_page_no = category_run.saved_page_count + 1
            if raw_page.page_no != expected_page_no:
                raise RuntimeError("category pages must be recorded continuously")
            if raw_page.page_no > target_page_count:
                raise ValueError("page number exceeds the target page count")
            if raw_page.page_no == 1:
                # 首页第一次确定 total 与目标页数，之后不可改变。
                if (
                    category_run.api_total is not None
                    or category_run.target_page_count is not None
                ):
                    raise RuntimeError("category pagination plan is already initialized")
                category_run.api_total = api_total
                category_run.target_page_count = target_page_count
            elif (
                category_run.api_total != api_total
                or category_run.target_page_count != target_page_count
            ):
                raise RuntimeError("category pagination plan changed after page one")
            # 首页赋值后也统一校验调用参数与持久化计划完全一致。
            if (
                category_run.api_total != api_total
                or category_run.target_page_count != target_page_count
            ):
                raise RuntimeError("category pagination plan is inconsistent")
            # 累计条数不能超过接口首页声明的 total。
            projected_item_count = category_run.saved_item_count + raw_page.item_count
            if projected_item_count > api_total:
                raise ValueError("saved item count exceeds api total")
            # raw 文件已经存在后才允许登记数据库索引。
            session.add(
                RawResponse(
                    category_run_id=category_run_id,
                    page_no=raw_page.page_no,
                    path=str(raw_page.path),
                    item_count=raw_page.item_count,
                    captured_at=stored_captured_at,
                )
            )
            # 分类进度与批次累计统计在同一事务内推进。
            category_run.saved_page_count = raw_page.page_no
            category_run.saved_item_count = projected_item_count
            self._recalculate_batch_counts(session, batch)
            # 返回完整快照，调用方只需一次 Manifest 原子同步。
            snapshot = self._collection_snapshot_from_session(session, batch)
        return snapshot

    def finish_category_success(
        self,
        category_run_id: str,
        api_total: int,
        target_page_count: int,
        finished_at: datetime,
    ) -> BatchCollectionSnapshot:
        """Finish one category only when every planned raw page is durable."""

        if api_total < 0:
            raise ValueError("api total cannot be negative")
        if target_page_count < 1:
            raise ValueError("target page count must be positive")
        # 成功时间统一转换为 SQLite 墙上时间。
        stored_finished_at = normalize_datetime(finished_at)
        with self.session_factory.begin() as session:
            # 分类和批次都必须仍在运行。
            batch, category_run = self._running_category_context(
                session,
                category_run_id,
            )
            # 调用方提供的计划只能验证，不能覆盖首页持久化值。
            if (
                category_run.api_total != api_total
                or category_run.target_page_count != target_page_count
            ):
                raise RuntimeError("category pagination plan does not match page one")
            if category_run.saved_page_count != target_page_count:
                raise RuntimeError("category has not saved every target page")
            if category_run.saved_item_count != api_total:
                raise RuntimeError("category saved item count does not match api total")
            # success 状态、完成时间和批次统计一次提交。
            category_run.status = "success"
            category_run.finished_at = stored_finished_at
            self._recalculate_batch_counts(session, batch)
            snapshot = self._collection_snapshot_from_session(session, batch)
        return snapshot

    def finish_category_failure(
        self,
        category_run_id: str,
        failed_page: int,
        error_category: str,
        finished_at: datetime,
    ) -> BatchCollectionSnapshot:
        """Finish one ordinary category failure and keep collecting other categories."""

        if failed_page < 1:
            raise ValueError("failed page must be positive")
        if not error_category:
            raise ValueError("category failure requires an error category")
        # 普通失败完成时间统一转换后写入分类行。
        stored_finished_at = normalize_datetime(finished_at)
        with self.session_factory.begin() as session:
            # 分类和批次都必须仍在运行。
            batch, category_run = self._running_category_context(
                session,
                category_run_id,
            )
            # 完整榜单校验可在最后一页已登记后失败，请求失败则发生在下一页。
            allowed_failed_pages = {
                max(1, category_run.saved_page_count),
                category_run.saved_page_count + 1,
            }
            if failed_page not in allowed_failed_pages:
                raise RuntimeError("failed page is inconsistent with saved progress")
            # 普通失败只结束当前分类，批次继续保持 running 以完成其余分类。
            category_run.status = "failed"
            category_run.failed_page = failed_page
            category_run.error_category = error_category
            category_run.finished_at = stored_finished_at
            self._recalculate_batch_counts(session, batch)
            snapshot = self._collection_snapshot_from_session(session, batch)
        return snapshot

    def terminate_collection_batch(
        self,
        batch_id: str,
        status: str,
        error_category: str,
        finished_at: datetime,
        current_category_run_id: str | None = None,
        failed_page: int | None = None,
    ) -> BatchCollectionSnapshot:
        """Atomically close the current category, pending categories, and batch."""

        # 采集期只允许进入这些非发布终态。
        allowed_statuses = {"failed", "auth_required", "interrupted", "abandoned"}
        if status not in allowed_statuses:
            raise ValueError("unsupported collection batch terminal status")
        if not error_category:
            raise ValueError("collection batch termination requires an error category")
        if failed_page is not None and failed_page < 1:
            raise ValueError("failed page must be positive")
        # 所有分类和批次共用同一完成时刻。
        stored_finished_at = normalize_datetime(finished_at)
        with self.session_factory.begin() as session:
            # 批次只能从 running 被一次性收口。
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise RuntimeError("collection batch does not exist")
            if batch.status != "running":
                raise RuntimeError("collection batch is not running")
            # 一级分类并发允许批次同时存在多个运行中分类。
            running_category_runs = session.scalars(
                select(CategoryRun).where(
                    CategoryRun.batch_id == batch_id,
                    CategoryRun.status == "running",
                )
            ).all()
            if current_category_run_id is None:
                if running_category_runs:
                    raise RuntimeError("running category id is required for termination")
                if failed_page is not None:
                    raise ValueError("failed page requires a current category run")
            else:
                # 当前分类必须是本批次的一个 running 分类。
                current_category_run = next(
                    (
                        category_run
                        for category_run in running_category_runs
                        if category_run.id == current_category_run_id
                    ),
                    None,
                )
                if current_category_run is None:
                    raise RuntimeError("current category run is not the running category")
                # 当前分类可能没有成功页，也可能在后续页终止。
                # 最后一页完整性校验失败和下一页请求失败都属于合法失败位置。
                allowed_failed_pages = {
                    max(1, current_category_run.saved_page_count),
                    current_category_run.saved_page_count + 1,
                }
                if (
                    failed_page is not None
                    and failed_page not in allowed_failed_pages
                ):
                    raise RuntimeError("failed page is inconsistent with saved progress")
                # 人工中止和放弃使用独立分类终态，其余终止均记为 failed。
                if status == "interrupted":
                    current_category_status = "interrupted"
                elif status == "abandoned":
                    current_category_status = "abandoned"
                else:
                    current_category_status = "failed"
                current_category_run.status = current_category_status
                current_category_run.failed_page = failed_page
                current_category_run.error_category = error_category
                current_category_run.finished_at = stored_finished_at
                # 其他并行中的分类没有独立失败原因，按批次终态一起安全收口。
                for running_category_run in running_category_runs:
                    if running_category_run.id == current_category_run_id:
                        continue
                    running_category_run.status = current_category_status
                    running_category_run.error_category = error_category
                    running_category_run.finished_at = stored_finished_at
            # 所有尚未启动分类与当前分类、批次在同一事务内收口。
            pending_category_runs = session.scalars(
                select(CategoryRun).where(
                    CategoryRun.batch_id == batch_id,
                    CategoryRun.status == "pending",
                )
            ).all()
            for pending_category_run in pending_category_runs:
                pending_category_run.status = "not_started"
                pending_category_run.finished_at = stored_finished_at
            # 批次终态不占用发布版本，error_category 只保存稳定分类。
            batch.status = status
            batch.error_category = error_category
            batch.finished_at = stored_finished_at
            self._recalculate_batch_counts(session, batch)
            snapshot = self._collection_snapshot_from_session(session, batch)
        return snapshot

    def finalize_dry_run(
        self,
        collected_batch: CollectedCategoryBatch,
        finished_at: datetime,
    ) -> BatchCollectionSnapshot:
        """Finalize a validated dry-run without official rows, CSV, or publication time."""

        # dry-run 完成时间按 SQLite 北京墙上时间保存。
        stored_finished_at = normalize_datetime(finished_at)
        try:
            with self.session_factory.begin() as session:
                # 公共发布校验确保内存只包含全部 success 分类。
                batch, category_runs, final_status = self._validate_collected_batch(
                    session,
                    collected_batch,
                )
                if batch.mode != "dry_run":
                    raise PublicationError(
                        "only a dry-run batch can use dry-run finalization",
                        category="publication_contract_error",
                    )
                # dry-run 永远不允许出现正式商品记录。
                self._ensure_no_product_entries(session, category_runs)
                # 最终状态完全由 SQLite 分类终态推导。
                batch.status = final_status
                batch.version = None
                batch.csv_path = None
                batch.published_at = None
                batch.error_category = None
                batch.finished_at = stored_finished_at
                # 返回快照供同一个 BatchStorage 原子更新 Manifest。
                snapshot = self._collection_snapshot_from_session(session, batch)
        except PublicationError:
            raise
        except Exception as error:
            raise PublicationError(
                "failed to finalize dry-run batch",
                category="dry_run_finalize_error",
            ) from error
        return snapshot

    def _official_publication_is_committed(
        self,
        *,
        batch_id: str,
        version: int,
        final_path: Path,
        published_at: datetime,
    ) -> bool:
        """Confirm that SQLite durably owns the exact official publication."""

        with self.session_factory() as session:
            # 新事务只接受相同批次、版本、CSV 和发布时间的正式成功终态。
            committed_batch_id = session.scalar(
                select(CollectionBatch.id)
                .where(
                    CollectionBatch.id == batch_id,
                    CollectionBatch.status.in_(("success", "partial_success")),
                    CollectionBatch.version == version,
                    CollectionBatch.csv_path == str(final_path),
                    CollectionBatch.published_at == published_at,
                )
                .limit(1)
            )
        return committed_batch_id is not None

    def publish_collected_batch(
        self,
        collected_batch: CollectedCategoryBatch,
        version: int,
        staged_csv: _StagedCsvExportLike,
        published_at: datetime,
    ) -> PublishedCollectionResult:
        """Publish official rows and CSV with rollback on every handled failure."""

        if version < 1:
            # 非法版本也应清理本次 prepare 创建的临时 CSV。
            _rollback_staged_csv_or_raise(staged_csv)
            raise PublicationError(
                "publication version must be positive",
                category="publication_contract_error",
            )
        # published_at 非空是正式发布唯一判据，统一保存为墙上时间。
        stored_published_at = normalize_datetime(published_at)
        # 该标记区分事务体内失败与完整终态进入 commit 后的边界中止。
        publication_ready_to_commit = False
        try:
            with self.session_factory.begin() as session:
                # SQLite 分类终态决定 success 或 partial_success。
                batch, category_runs, final_status = self._validate_collected_batch(
                    session,
                    collected_batch,
                )
                if batch.mode not in {"normal", "force"}:
                    raise PublicationError(
                        "dry-run batch cannot be officially published",
                        category="publication_contract_error",
                    )
                # 同一批次禁止在正式发布前残留任何商品记录。
                self._ensure_no_product_entries(session, category_runs)
                # task+planned_at+version 必须由当前批次唯一占用。
                conflicting_batch_id = session.scalar(
                    select(CollectionBatch.id)
                    .where(
                        CollectionBatch.task_id == batch.task_id,
                        CollectionBatch.planned_at == batch.planned_at,
                        CollectionBatch.version == version,
                        CollectionBatch.id != batch.id,
                    )
                    .limit(1)
                )
                if conflicting_batch_id is not None:
                    raise PublicationError(
                        "publication version is already in use",
                        category="version_conflict",
                    )
                # publishing 状态先占用版本和 CSV 路径，但仍没有 published_at。
                batch.status = "publishing"
                batch.version = version
                batch.csv_path = str(staged_csv.final_path)
                batch.published_at = None
                batch.finished_at = None
                batch.error_category = None
                session.flush()
                # 只为全部 success 分类写入官方商品和店铺关系。
                self._insert_product_entries(session, collected_batch)
                # CSV 发布前 flush 所有行，提前触发唯一约束并保留可回滚事务。
                session.flush()
                staged_csv.publish()
                # CSV 已原子发布后写入正式终态，提交失败会由 rollback 删除 CSV。
                batch.status = final_status
                batch.finished_at = normalize_datetime(collected_batch.finished_at)
                batch.published_at = stored_published_at
                self._recalculate_batch_counts(session, batch)
                snapshot = self._collection_snapshot_from_session(session, batch)
                # 幂等摘要与最终快照共享同一事务中的批次字段。
                published_batch = PublishedBatch(
                    batch_id=batch.id,
                    task_id=batch.task_id,
                    planned_at=batch.planned_at,
                    version=version,
                    csv_path=staged_csv.final_path,
                )
                # 返回对象也在提交前完整构造，任何异常都仍可回滚数据库和 CSV。
                publication_result = PublishedCollectionResult(
                    published_batch=published_batch,
                    snapshot=snapshot,
                )
                # 返回对象和全部终态字段完成后，事务才具备提交正式发布的条件。
                publication_ready_to_commit = True
            return publication_result
        except BaseException as error:
            # begin 边界可能在真实 commit 后收到中止，必须用新事务读取权威终态。
            publication_committed = (
                publication_ready_to_commit
                and self._official_publication_is_committed(
                    batch_id=collected_batch.batch_id,
                    version=version,
                    final_path=staged_csv.final_path,
                    published_at=stored_published_at,
                )
            )
            if not publication_committed:
                # 未提交或提交失败时补偿临时 CSV 和本次已经移动的最终 CSV。
                _rollback_staged_csv_or_raise(staged_csv)
            if not isinstance(error, Exception):
                # 进程级中止保留原始退出语义，由 runner 根据 SQLite 终态收口。
                raise
            if isinstance(error, PublicationError):
                raise
            raise PublicationError(
                "failed to publish collected batch",
                category="publication_failed",
            ) from error

    def successful_batch(
        self,
        task_id: str,
        planned_at: datetime,
    ) -> PublishedBatch | None:
        """Return the latest officially published version for one planned task."""

        # 查询时间使用与入库一致的 SQLite 墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        with self.session_factory() as session:
            # published_at 非空是唯一正式发布判定，包含 partial_success。
            batch = session.scalar(
                select(CollectionBatch)
                .where(
                    CollectionBatch.task_id == task_id,
                    CollectionBatch.planned_at == stored_planned_at,
                    CollectionBatch.published_at.is_not(None),
                )
                .order_by(CollectionBatch.version.desc())
                .limit(1)
            )
        if batch is None or batch.version is None or batch.csv_path is None:
            return None
        return PublishedBatch(
            batch_id=batch.id,
            task_id=batch.task_id,
            planned_at=batch.planned_at,
            version=batch.version,
            csv_path=Path(batch.csv_path),
        )

    def next_version(self, task_id: str, planned_at: datetime) -> int:
        """Allocate the next immutable publication version."""

        # 查询时间使用与入库一致的 SQLite 墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        with self.session_factory() as session:
            # publishing 也会暂时占用版本，崩溃恢复在阶段五负责释放。
            maximum_version = session.scalar(
                select(func.max(CollectionBatch.version)).where(
                    CollectionBatch.task_id == task_id,
                    CollectionBatch.planned_at == stored_planned_at,
                )
            )
        return 1 if maximum_version is None else maximum_version + 1

    def has_terminal_run(self, task_id: str, planned_at: datetime) -> bool:
        """Return whether Scheduler already handled one planned execution."""

        # dry-run 不能阻止 Scheduler 的正式执行。
        stored_planned_at = normalize_datetime(planned_at)
        with self.session_factory() as session:
            # 任一非 dry-run 终态都遵守“Scheduler 不自动重试”。
            batch_id = session.scalar(
                select(CollectionBatch.id)
                .where(
                    CollectionBatch.task_id == task_id,
                    CollectionBatch.planned_at == stored_planned_at,
                    CollectionBatch.mode != "dry_run",
                    CollectionBatch.status.in_(TERMINAL_BATCH_STATUSES),
                )
                .limit(1)
            )
        return batch_id is not None

    def _record_scheduler_terminal_batch(
        self,
        *,
        task_id: str,
        business_date: date,
        planned_at: datetime,
        recorded_at: datetime,
        status: str,
        error_category: str,
    ) -> str | None:
        """Persist one Scheduler-only terminal batch without runtime artifacts."""

        # 计划时间和记录时间统一保存为 SQLite 墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        stored_recorded_at = normalize_datetime(recorded_at)
        # Scheduler-only 批次使用独立 UUID，可被日志和 status 稳定引用。
        batch_id = uuid4().hex
        with self.session_factory.begin() as session:
            # 已有任意非 dry-run 尝试时不重复造 Scheduler 终态。
            existing_batch_id = session.scalar(
                select(CollectionBatch.id)
                .where(
                    CollectionBatch.task_id == task_id,
                    CollectionBatch.planned_at == stored_planned_at,
                    CollectionBatch.mode != "dry_run",
                )
                .limit(1)
            )
            if existing_batch_id is not None:
                return None
            session.add(
                CollectionBatch(
                    id=batch_id,
                    task_id=task_id,
                    business_date=business_date,
                    planned_at=stored_planned_at,
                    mode="normal",
                status=status,
                version=None,
                brand_type=None,
                price_bin=None,
                root_category_id=None,
                    root_category_name=None,
                    manifest_path=None,
                    category_tree_raw_path=None,
                    csv_path=None,
                    discovered_category_count=0,
                    successful_category_count=0,
                    failed_category_count=0,
                    not_started_category_count=0,
                    saved_page_count=0,
                    collected_item_count=0,
                    error_category=error_category,
                    started_at=stored_recorded_at,
                    finished_at=stored_recorded_at,
                    published_at=None,
                )
            )
        return batch_id

    def record_missed_run(
        self,
        *,
        task_id: str,
        business_date: date,
        planned_at: datetime,
        error_category: str,
        recorded_at: datetime,
    ) -> str | None:
        """Persist one missed Scheduler occurrence as a batch terminal state."""

        return self._record_scheduler_terminal_batch(
            task_id=task_id,
            business_date=business_date,
            planned_at=planned_at,
            recorded_at=recorded_at,
            status="missed",
            error_category=error_category,
        )

    def record_skipped_busy_run(
        self,
        *,
        task_id: str,
        business_date: date,
        planned_at: datetime,
        recorded_at: datetime,
    ) -> str | None:
        """Persist one Scheduler occurrence skipped because collection is busy."""

        return self._record_scheduler_terminal_batch(
            task_id=task_id,
            business_date=business_date,
            planned_at=planned_at,
            recorded_at=recorded_at,
            status="skipped_busy",
            error_category="skipped_busy",
        )

    def scheduler_checkpoint(self, task_id: str) -> datetime | None:
        """Read one task's last durable reconciliation time."""

        with self.session_factory() as session:
            # 主键查询避免扫描其他任务调度状态。
            checkpoint = session.get(SchedulerCheckpoint, task_id)
        return checkpoint.last_checked_at if checkpoint is not None else None

    def set_scheduler_checkpoint(
        self,
        task_id: str,
        checked_at: datetime,
    ) -> None:
        """Upsert one task checkpoint after all due occurrences are handled."""

        # 检查时间按 SQLite 统一墙上时间保存。
        stored_checked_at = normalize_datetime(checked_at)
        with self.session_factory.begin() as session:
            # 主键存在时原地推进，不存在时初始化。
            checkpoint = session.get(SchedulerCheckpoint, task_id)
            if checkpoint is None:
                session.add(
                    SchedulerCheckpoint(
                        task_id=task_id,
                        last_checked_at=stored_checked_at,
                        updated_at=stored_checked_at,
                    )
                )
            else:
                checkpoint.last_checked_at = stored_checked_at
                checkpoint.updated_at = stored_checked_at

    def recent_status(self, limit: int = 20) -> list[StatusRow]:
        """Return recent batch attempts without expanding category details."""

        with self.session_factory() as session:
            # 顶层 status 直接按批次开始时间倒序读取。
            batches = session.scalars(
                select(CollectionBatch)
                .order_by(CollectionBatch.started_at.desc())
                .limit(limit)
            ).all()
        # 查询结果转为与 ORM Session 解耦的不可变摘要。
        return [
            StatusRow(
                batch_id=batch.id,
                task_id=batch.task_id,
                planned_at=batch.planned_at,
                mode=batch.mode,
                status=batch.status,
                version=batch.version,
                brand_type=batch.brand_type,
                price_bin=batch.price_bin,
                started_at=batch.started_at,
                finished_at=batch.finished_at,
                published_at=batch.published_at,
                error_category=batch.error_category,
                csv_path=batch.csv_path,
                discovered_category_count=batch.discovered_category_count,
                successful_category_count=batch.successful_category_count,
                failed_category_count=batch.failed_category_count,
                not_started_category_count=batch.not_started_category_count,
                saved_page_count=batch.saved_page_count,
                collected_item_count=batch.collected_item_count,
            )
            for batch in batches
        ]
