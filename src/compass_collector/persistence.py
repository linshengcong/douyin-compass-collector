"""SQLite schema, Alembic upgrades, idempotence, and transactional publication."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from compass_collector.errors import PublicationError
from compass_collector.exporter import StagedCsvExport
from compass_collector.models import CollectedTaskRun
from compass_collector.raw_storage import RunStorage, current_time_iso


class Base(DeclarativeBase):
    """Base metadata shared by ORM models and Alembic migrations."""


class CollectionBatch(Base):
    """Store one successfully published planned snapshot version."""

    __tablename__ = "collection_batches"
    __table_args__ = (
        UniqueConstraint("task_id", "planned_at", "version", name="uq_batch_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    csv_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CollectionRun(Base):
    """Store every stage-two task attempt, including failed attempts."""

    __tablename__ = "collection_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("collection_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(120), nullable=True)


class RawResponse(Base):
    """Index one validated raw response file without storing its JSON body."""

    __tablename__ = "raw_responses"
    __table_args__ = (
        UniqueConstraint("run_id", "page_no", name="uq_raw_response_page"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("collection_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProductRankEntryModel(Base):
    """Persist one official product ranking entry with raw metric values."""

    __tablename__ = "product_rank_entries"
    __table_args__ = (
        UniqueConstraint("batch_id", "rank", name="uq_batch_rank"),
        UniqueConstraint("batch_id", "product_id", name="uq_batch_product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("collection_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("collection_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_name: Mapped[str] = mapped_column(String(2048), nullable=False)
    newly_on_ranking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pay_amount_min_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_amount_max_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_amount_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    pay_combo_count_min_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_combo_count_max_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_combo_count_unit: Mapped[str] = mapped_column(String(32), nullable=False)


class ProductRankEntryShopModel(Base):
    """Persist every shop linked to a ranking entry in source order."""

    __tablename__ = "product_rank_entry_shops"
    __table_args__ = (
        UniqueConstraint("entry_id", "position", name="uq_entry_shop_position"),
    )

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
    # SQLite 保存北京时间无时区墙上时间，与其他计划字段一致。
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


@dataclass(frozen=True, slots=True)
class PublishedBatch:
    """Expose non-sensitive publication metadata to the runner and status command."""

    batch_id: str
    task_id: str
    planned_at: datetime
    version: int
    csv_path: Path


@dataclass(frozen=True, slots=True)
class StatusRow:
    """Represent one concise status-command row."""

    run_id: str
    task_id: str
    planned_at: datetime
    status: str
    version: int | None
    started_at: datetime
    finished_at: datetime
    error_category: str | None
    csv_path: str | None


def normalize_datetime(value: datetime) -> datetime:
    """Store Beijing wall-clock datetimes consistently in SQLite."""

    # SQLite 不保存时区偏移，入库前转为无时区的北京墙上时间。
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
    # 项目根目录从当前包文件稳定推导。
    project_root = Path(__file__).resolve().parents[2]
    # Alembic 配置在运行时覆盖为当前数据库绝对 URL。
    alembic_config = AlembicConfig(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url(database_path))
    command.upgrade(alembic_config, "head")


class Database:
    """Provide idempotence queries and one deep transactional publication operation."""

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

    def successful_batch(
        self,
        task_id: str,
        planned_at: datetime,
    ) -> PublishedBatch | None:
        """Return the latest successful version for one planned task execution."""

        # 查询时间使用与入库一致的 SQLite 墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        with self.session_factory() as session:
            # 最新成功版本决定默认 run 是否跳过。
            batch = session.scalar(
                select(CollectionBatch)
                .where(
                    CollectionBatch.task_id == task_id,
                    CollectionBatch.planned_at == stored_planned_at,
                    CollectionBatch.status == "success",
                )
                .order_by(CollectionBatch.version.desc())
                .limit(1)
            )
        if batch is None:
            return None
        return PublishedBatch(
            batch_id=batch.id,
            task_id=batch.task_id,
            planned_at=batch.planned_at,
            version=batch.version,
            csv_path=Path(batch.csv_path),
        )

    def next_version(self, task_id: str, planned_at: datetime) -> int:
        """Allocate the next immutable version for a forced publication."""

        # 查询时间使用与入库一致的 SQLite 墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        with self.session_factory() as session:
            # 版本号基于同一计划执行的已发布最大值递增。
            maximum_version = session.scalar(
                select(func.max(CollectionBatch.version)).where(
                    CollectionBatch.task_id == task_id,
                    CollectionBatch.planned_at == stored_planned_at,
                )
            )
        return 1 if maximum_version is None else maximum_version + 1

    def has_terminal_run(self, task_id: str, planned_at: datetime) -> bool:
        """Return whether Scheduler has any completed attempt for a planned time."""

        # Scheduler 与人工补跑不同：任意终态都阻止自动再次执行。
        stored_planned_at = normalize_datetime(planned_at)
        with self.session_factory() as session:
            # collection_runs 只在任务已结束后写入，因此存在即为终态。
            run_id = session.scalar(
                select(CollectionRun.id)
                .where(
                    CollectionRun.task_id == task_id,
                    CollectionRun.planned_at == stored_planned_at,
                )
                .limit(1)
            )
        return run_id is not None

    def record_missed_run(
        self,
        *,
        task_id: str,
        business_date: date,
        planned_at: datetime,
        error_category: str,
        recorded_at: datetime,
    ) -> str | None:
        """Persist one synthetic missed terminal state without raw runtime files."""

        # 检查与写入在同一事务中完成，单进程 Scheduler 下保持幂等。
        stored_planned_at = normalize_datetime(planned_at)
        # missed run 使用独立 UUID，可被 status 稳定引用。
        run_id = uuid4().hex
        with self.session_factory.begin() as session:
            # 已有成功、失败、鉴权或 missed 状态时不重复造记录。
            existing_run_id = session.scalar(
                select(CollectionRun.id)
                .where(
                    CollectionRun.task_id == task_id,
                    CollectionRun.planned_at == stored_planned_at,
                )
                .limit(1)
            )
            if existing_run_id is not None:
                return None
            # missed 没有浏览器、HTTP 或原始响应目录。
            stored_recorded_at = normalize_datetime(recorded_at)
            session.add(
                CollectionRun(
                    id=run_id,
                    batch_id=None,
                    task_id=task_id,
                    business_date=business_date,
                    planned_at=stored_planned_at,
                    status="missed",
                    started_at=stored_recorded_at,
                    finished_at=stored_recorded_at,
                    error_category=error_category,
                )
            )
        return run_id

    def scheduler_checkpoint(self, task_id: str) -> datetime | None:
        """Read one task's last durable reconciliation time."""

        with self.session_factory() as session:
            # 主键查询避免扫描其他任务的调度状态。
            checkpoint = session.get(SchedulerCheckpoint, task_id)
        return checkpoint.last_checked_at if checkpoint is not None else None

    def set_scheduler_checkpoint(
        self,
        task_id: str,
        checked_at: datetime,
    ) -> None:
        """Upsert one task checkpoint only after its due occurrences are handled."""

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

    def record_failed_run(
        self,
        storage: RunStorage,
        *,
        planned_at: datetime,
        error_category: str,
    ) -> None:
        """Persist a failed attempt without creating an official batch snapshot."""

        # Manifest 时间戳已是带时区 ISO 格式。
        started_at = datetime.fromisoformat(storage.manifest["started_at"])
        # 失败记录在 Manifest 终态写入后创建。
        finished_at_text = storage.manifest.get("finished_at") or current_time_iso()
        finished_at = datetime.fromisoformat(finished_at_text)
        with self.session_factory.begin() as session:
            # 同一 run_id 只能记录一次，避免异常处理重入。
            existing_run = session.get(CollectionRun, storage.run_id)
            if existing_run is not None:
                return
            session.add(
                CollectionRun(
                    id=storage.run_id,
                    batch_id=None,
                    task_id=storage.task_id,
                    business_date=storage.business_date,
                    planned_at=normalize_datetime(planned_at),
                    status=storage.manifest["status"],
                    started_at=normalize_datetime(started_at),
                    finished_at=normalize_datetime(finished_at),
                    error_category=error_category,
                )
            )

    def publish_snapshot(
        self,
        collected_run: CollectedTaskRun,
        *,
        planned_at: datetime,
        version: int,
        staged_csv: StagedCsvExport,
    ) -> PublishedBatch:
        """Publish database rows and the staged CSV as one coordinated transaction."""

        # 批次 ID 与 run_id 分离，为后续一个计划批次包含多任务保留边界。
        batch_id = uuid4().hex
        # 入库计划时间统一转为 SQLite 墙上时间。
        stored_planned_at = normalize_datetime(planned_at)
        # 批次完成时间使用已完成采集和校验的时刻。
        stored_finished_at = normalize_datetime(collected_run.finished_at)
        try:
            with self.session_factory.begin() as session:
                # 官方快照批次只在整榜和 CSV 均已准备好时创建。
                batch = CollectionBatch(
                    id=batch_id,
                    task_id=collected_run.task_id,
                    business_date=collected_run.business_date,
                    planned_at=stored_planned_at,
                    version=version,
                    status="success",
                    csv_path=str(staged_csv.final_path),
                    created_at=normalize_datetime(collected_run.started_at),
                    finished_at=stored_finished_at,
                )
                session.add(batch)
                # 成功 run 与官方批次关联。
                session.add(
                    CollectionRun(
                        id=collected_run.storage.run_id,
                        batch_id=batch_id,
                        task_id=collected_run.task_id,
                        business_date=collected_run.business_date,
                        planned_at=stored_planned_at,
                        status="success",
                        started_at=normalize_datetime(collected_run.started_at),
                        finished_at=stored_finished_at,
                        error_category=None,
                    )
                )
                for raw_page in collected_run.raw_pages:
                    session.add(
                        RawResponse(
                            run_id=collected_run.storage.run_id,
                            page_no=raw_page.page_no,
                            path=str(raw_page.path),
                            item_count=raw_page.item_count,
                            captured_at=normalize_datetime(raw_page.captured_at),
                        )
                    )
                for entry in collected_run.entries:
                    # 商品主记录先 flush 获取主键，再写入全部店铺关系。
                    entry_model = ProductRankEntryModel(
                        batch_id=batch_id,
                        run_id=collected_run.storage.run_id,
                        task_id=collected_run.task_id,
                        business_date=collected_run.business_date,
                        planned_at=stored_planned_at,
                        captured_at=normalize_datetime(entry.captured_at),
                        page_no=entry.page_no,
                        rank=entry.rank,
                        product_id=entry.product_id,
                        product_name=entry.product_name,
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
                        session.add(
                            ProductRankEntryShopModel(
                                entry_id=entry_model.id,
                                position=shop.position,
                                shop_id=shop.shop_id,
                                shop_name=shop.shop_name,
                            )
                        )
                # CSV 原子发布发生在数据库 commit 之前；任何异常会回滚两边。
                staged_csv.publish()
        except Exception as error:
            staged_csv.rollback()
            if isinstance(error, PublicationError):
                raise
            if isinstance(error, SQLAlchemyError):
                raise PublicationError(
                    "database transaction failed",
                    category="database_error",
                ) from error
            raise
        return PublishedBatch(
            batch_id=batch_id,
            task_id=collected_run.task_id,
            planned_at=stored_planned_at,
            version=version,
            csv_path=staged_csv.final_path,
        )

    def recent_status(self, limit: int = 20) -> list[StatusRow]:
        """Return recent run attempts with optional publication metadata."""

        with self.session_factory() as session:
            # 外连接保留没有成功批次的失败 run。
            rows = session.execute(
                select(
                    CollectionRun,
                    CollectionBatch.version,
                    CollectionBatch.csv_path,
                )
                .outerjoin(CollectionBatch, CollectionRun.batch_id == CollectionBatch.id)
                .order_by(CollectionRun.started_at.desc())
                .limit(limit)
            ).all()
        # 查询结果转为与 ORM Session 解耦的不可变摘要。
        status_rows: list[StatusRow] = []
        for run, version, csv_path in rows:
            status_rows.append(
                StatusRow(
                    run_id=run.id,
                    task_id=run.task_id,
                    planned_at=run.planned_at,
                    status=run.status,
                    version=version,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    error_category=run.error_category,
                    csv_path=csv_path,
                )
            )
        return status_rows
