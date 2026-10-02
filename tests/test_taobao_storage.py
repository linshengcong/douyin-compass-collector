"""Synthetic Taobao response → transactional SQLite → platform CSV checks."""

import csv
from dataclasses import replace
from decimal import Decimal

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from compass_collector.exporter import CsvExporter, TAOBAO_CSV_HEADERS
from compass_collector.persistence import CollectionBatch, ProductRankEntryModel, ProductRankEntryShopModel, upgrade_database
from compass_collector.platforms.taobao_product_rank import parse_page_entries
from test_stage_four_persistence import prepare_collected_batch, PUBLISHED_AT
from test_taobao_product_rank import page_payload


def test_taobao_publication_preserves_source_and_missing_values(tmp_path):
    """Publish a parsed synthetic row through the real migration and transaction."""
    # 复用已完成分类的受控生命周期，只替换该分类的合成业务响应。
    database, collected = prepare_collected_batch(tmp_path, mode="normal", category_statuses=("success",))
    # raw 条数和解析条数一致，避免测试绕过正式发布完整性校验。
    category = collected.category_runs[0]
    # 解析器生成未知首次上榜、空访客数及不能伪装为 shop_id 的卖家 ID。
    entries = parse_page_entries(page_payload(total=1), page_no=1, captured_at=category.entries[0].captured_at)
    collected = replace(collected, category_runs=(replace(category, entries=tuple(entries)),))
    try:
        with database.session_factory.begin() as session:
            # 仅修改隔离测试数据库，生产平台来源应由任务创建时确定。
            batch = session.get(CollectionBatch, collected.batch_id)
            batch.platform = "taobao"
        # 使用生产 CSV 写入器及事务发布器，验证真实字段与 BOM。
        staged = CsvExporter(tmp_path / "exports").prepare(
            task_id=collected.task_id, display_name="淘宝合成验收", planned_at=collected.started_at,
            version=1, batch_id=collected.batch_id, category_runs=collected.category_runs, platform="taobao",
        )
        database.publish_collected_batch(collected, 1, staged, PUBLISHED_AT)
        # 回到有真实淘宝商品/店铺的旧版后重新升级，验证平台回填和外键保留。
        database.close()
        migration_config = Config("alembic.ini")
        migration_config.set_main_option("sqlalchemy.url", str(database.engine.url))
        command.downgrade(migration_config, "0006_taobao_metrics")
        upgrade_database(tmp_path / "runtime" / "data" / "collector.db")
        with database.session_factory() as session:
            # 从 SQLite 读取而非仅检查内存，证明空值与原始区间完整落库。
            product = session.scalar(select(ProductRankEntryModel))
            shop = session.scalar(select(ProductRankEntryShopModel))
            assert product.platform == "taobao" and shop.entry_id == product.id
            assert product.pay_buyer_count_raw == "2.5万 ~ 5万"
            assert product.pay_buyer_count_min_value == Decimal(25000)
            assert product.pay_buyer_count_max_value == Decimal(50000)
            assert product.visitor_count_raw is None and product.visitor_count_min_value is None
            assert product.pay_amount_min_value is None and product.pay_combo_count_min_value is None
            assert product.newly_on_ranking is None
            assert product.product_url == entries[0].product_url
            assert shop.shop_id is None and shop.seller_user_id == "123"
            assert shop.shop_url == "https://synthetic.tmall.com"
        assert staged.final_path.read_bytes().startswith(b"\xef\xbb\xbf")
        with staged.final_path.open(encoding="utf-8-sig", newline="") as handle:
            # DictReader 也检查商品链接和空访客展示，不让原始区间被格式化改变。
            reader = csv.DictReader(handle)
            assert tuple(reader.fieldnames) == TAOBAO_CSV_HEADERS
            row = next(reader)
            assert row["支付买家数"] == "2.5万 ~ 5万"
            assert row["访客数"] == ""
            assert row["商品链接"] == entries[0].product_url
    finally:
        database.close()
