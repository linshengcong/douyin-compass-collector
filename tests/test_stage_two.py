"""Stage-two parser, migration, transaction, idempotence, and CSV tests."""

import csv
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from compass_collector.errors import PublicationError
from compass_collector.exporter import CsvExporter, format_metric_range
from compass_collector.models import CollectedTaskRun, RawPageRecord
from compass_collector.persistence import (
    CollectionBatch,
    CollectionRun,
    Database,
    ProductRankEntryModel,
    ProductRankEntryShopModel,
    RawResponse,
    upgrade_database,
)
from compass_collector.product_rank import (
    parse_page_entries,
    validate_complete_ranking,
)
from compass_collector.raw_storage import RunStorage


# 阶段二仍只使用用户提供真实响应的脱敏副本。
FIXTURE_PATH = Path("tests/fixtures/product_rank_page.json")
# 测试时间使用工程确认的北京时区。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def load_real_entries():
    """Parse ten entries from the single sanitized real-response fixture."""

    # 脱敏真实响应在测试进程内加载。
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 固定捕获时间使测试结果可重复。
    captured_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    # 真实首页解析为领域记录。
    entries = parse_page_entries(payload, page_no=1, captured_at=captured_at)
    return payload, captured_at, entries


def build_collected_run(tmp_path: Path) -> CollectedTaskRun:
    """Build a publishable ten-row run from the real fixture and local raw storage."""

    # 真实 Fixture 提供原始页与已解析商品。
    payload, captured_at, entries = load_real_entries()
    validate_complete_ranking(entries, target_items=10)
    # 测试 runtime 与工程真实 runtime 完全隔离。
    storage = RunStorage(
        runtime_root=tmp_path / "runtime",
        task_id="product_hot_sale_drinks",
        business_date=date(2026, 7, 16),
        max_items=10,
    )
    # 真实页响应通过 gzip 原子写入后再索引。
    page_path = storage.write_page(1, payload)
    storage.update_progress(
        api_total=200,
        target_items=10,
        saved_pages=1,
        saved_items=10,
    )
    # 采集运行时间范围使用固定值便于数据库断言。
    started_at = datetime(2026, 7, 16, 13, 59, tzinfo=SHANGHAI_TIMEZONE)
    finished_at = datetime(2026, 7, 16, 14, 1, tzinfo=SHANGHAI_TIMEZONE)
    return CollectedTaskRun(
        task_id="product_hot_sale_drinks",
        business_date=date(2026, 7, 16),
        started_at=started_at,
        finished_at=finished_at,
        storage=storage,
        entries=tuple(entries),
        raw_pages=(
            RawPageRecord(
                page_no=1,
                path=page_path,
                item_count=10,
                captured_at=captured_at,
            ),
        ),
    )


def test_real_fixture_parses_and_formats_like_the_agreed_csv_contract() -> None:
    """Preserve raw values while formatting only the CSV presentation layer."""

    # 真实 Fixture 解析结果用于核对首行字段和展示换算。
    _, _, entries = load_real_entries()
    validate_complete_ranking(entries, target_items=10)
    # 首名商品包含金额和成交件数的已验证区间。
    first_entry = entries[0]

    assert first_entry.rank == 1
    assert first_entry.pay_amount.min_value == 1_000_000_000
    assert first_entry.pay_combo_count.min_value == 100_000
    assert format_metric_range(first_entry.pay_amount) == "¥1000万-¥2500万"
    assert format_metric_range(first_entry.pay_combo_count) == "1万-2.5万"


def test_migration_transaction_csv_and_idempotence_use_the_real_fixture(
    tmp_path: Path,
) -> None:
    """Publish one real-fixture snapshot and reject a duplicate transaction."""

    # 临时 SQLite 路径用于真实执行 Alembic 迁移。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    # 数据库对象管理幂等查询和单事务发布。
    database = Database(database_path)
    # 同一真实 Fixture 构建完整可发布 run。
    collected_run = build_collected_run(tmp_path)
    # 计划时间对应当天北京时间 14:00。
    planned_at = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI_TIMEZONE)
    # CSV 导出器只在测试临时 runtime 中生成文件。
    exporter = CsvExporter(tmp_path / "runtime" / "exports")
    # 第一个临时 CSV 将在数据库事务内原子发布。
    staged_csv = exporter.prepare(
        task_id=collected_run.task_id,
        planned_at=planned_at,
        version=1,
        run_id=collected_run.storage.run_id,
        entries=collected_run.entries,
    )
    try:
        # 首次发布应生成 v1 官方快照。
        published_batch = database.publish_snapshot(
            collected_run,
            planned_at=planned_at,
            version=1,
            staged_csv=staged_csv,
        )
        # 官方 CSV 使用 BOM 编码便于常见表格工具识别中文。
        with published_batch.csv_path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file_handle:
            csv_rows = list(csv.reader(file_handle))
        with database.session_factory() as session:
            # 各表行数用于验证整个发布链路。
            table_counts = {
                "batches": session.scalar(select(func.count()).select_from(CollectionBatch)),
                "runs": session.scalar(select(func.count()).select_from(CollectionRun)),
                "raw": session.scalar(select(func.count()).select_from(RawResponse)),
                "entries": session.scalar(
                    select(func.count()).select_from(ProductRankEntryModel)
                ),
                "shops": session.scalar(
                    select(func.count()).select_from(ProductRankEntryShopModel)
                ),
            }
        # 幂等查询必须找到刚发布的 v1。
        successful_batch = database.successful_batch(collected_run.task_id, planned_at)

        assert table_counts == {
            "batches": 1,
            "runs": 1,
            "raw": 1,
            "entries": 10,
            "shops": 10,
        }
        assert csv_rows[0] == ["排名", "商品", "店铺名称", "用户支付金额", "成交件数", "首次上榜"]
        assert csv_rows[1][3:] == ["¥1000万-¥2500万", "1万-2.5万", "false"]
        assert len(csv_rows) == 11
        assert successful_batch is not None
        assert successful_batch.version == 1
        assert database.next_version(collected_run.task_id, planned_at) == 2

        # 重复 v1 使用同一真实数据验证数据库和 CSV 回滚。
        duplicate_csv = exporter.prepare(
            task_id=collected_run.task_id,
            planned_at=planned_at,
            version=1,
            run_id=collected_run.storage.run_id,
            entries=collected_run.entries,
        )
        with pytest.raises(PublicationError):
            database.publish_snapshot(
                collected_run,
                planned_at=planned_at,
                version=1,
                staged_csv=duplicate_csv,
            )
        with database.session_factory() as session:
            # 回滚后官方批次数必须仍为 1。
            batch_count_after_rollback = session.scalar(
                select(func.count()).select_from(CollectionBatch)
            )
        assert batch_count_after_rollback == 1
        assert published_batch.csv_path.exists()
        assert not duplicate_csv.temporary_path.exists()
    finally:
        database.close()
