"""Add nullable Taobao metrics while preserving existing Compass records."""

import sqlalchemy as sa
from alembic import op

# 新字段独立迁移，历史单位转换仍只由 0005 执行一次。
revision = "0006_taobao_metrics"
down_revision = "0005_platform_capture"
branch_labels = None
depends_on = None

# 罗盘指标在淘宝记录上不适用，因此允许完整空值。
LEGACY_COLUMNS = {
    "newly_on_ranking": sa.Boolean(),
    "pay_amount_min_value": sa.Numeric(24, 4),
    "pay_amount_max_value": sa.Numeric(24, 4),
    "pay_amount_unit": sa.String(32),
    "pay_combo_count_min_value": sa.Numeric(24, 4),
    "pay_combo_count_max_value": sa.Numeric(24, 4),
    "pay_combo_count_unit": sa.String(32),
}
# 淘宝文本展示与数值筛选使用同一份响应来源。
METRICS = ("pay_buyer_count", "visitor_count")


def upgrade() -> None:
    """Preserve historical values and add independent Taobao columns."""
    with op.batch_alter_table("product_rank_entries") as batch:
        for name, column_type in LEGACY_COLUMNS.items():
            batch.alter_column(name, existing_type=column_type, nullable=True)
        batch.add_column(sa.Column("product_url", sa.String(4096), nullable=True))
        for metric in METRICS:
            batch.add_column(sa.Column(f"{metric}_raw", sa.String(128), nullable=True))
            for boundary in ("min_value", "max_value"):
                batch.add_column(sa.Column(f"{metric}_{boundary}", sa.Numeric(24, 4), nullable=True))
            batch.add_column(sa.Column(f"{metric}_unit", sa.String(32), nullable=True))
            batch.create_check_constraint(
                f"ck_product_rank_entries_{metric}",
                f"({metric}_min_value IS NULL AND {metric}_max_value IS NULL AND {metric}_unit IS NULL) OR "
                f"({metric}_min_value IS NOT NULL AND {metric}_max_value IS NOT NULL AND {metric}_unit IS NOT NULL "
                f"AND {metric}_unit = 'count' AND {metric}_min_value >= 0 AND {metric}_min_value <= {metric}_max_value)",
            )
    with op.batch_alter_table("product_rank_entry_shops") as batch:
        batch.alter_column("shop_id", existing_type=sa.String(128), nullable=True)
        batch.add_column(sa.Column("shop_url", sa.String(4096), nullable=True))
        batch.add_column(sa.Column("seller_user_id", sa.String(128), nullable=True))


def downgrade() -> None:
    """Reject loss of Taobao data instead of fabricating legacy values."""
    # 降级前检查新字段及空值，避免回滚迁移默默丢弃已采集数据。
    connection = op.get_bind()
    # 任一扩展字段都可能包含淘宝来源数据，必须先完整导出再人工处理。
    extension_columns = ["product_url"] + [
        f"{metric}_{suffix}"
        for metric in METRICS
        for suffix in ("raw", "min_value", "max_value", "unit")
    ]
    # 列名均来自固定常量，不接收外部 SQL 标识符。
    conditions = [f"{name} IS NULL" for name in LEGACY_COLUMNS]
    conditions.extend(f"{name} IS NOT NULL" for name in extension_columns)
    if connection.scalar(sa.text("SELECT COUNT(*) FROM product_rank_entries WHERE " + " OR ".join(conditions))):
        raise RuntimeError("cannot downgrade while Taobao ranking data exists")
    if connection.scalar(sa.text("SELECT COUNT(*) FROM product_rank_entry_shops WHERE shop_id IS NULL OR shop_url IS NOT NULL OR seller_user_id IS NOT NULL")):
        raise RuntimeError("cannot downgrade while Taobao shop data exists")
    with op.batch_alter_table("product_rank_entry_shops") as batch:
        batch.drop_column("seller_user_id")
        batch.drop_column("shop_url")
        batch.alter_column("shop_id", existing_type=sa.String(128), nullable=False)
    with op.batch_alter_table("product_rank_entries") as batch:
        for metric in METRICS:
            batch.drop_constraint(f"ck_product_rank_entries_{metric}", type_="check")
        for name in extension_columns:
            batch.drop_column(name)
        for name, column_type in LEGACY_COLUMNS.items():
            batch.alter_column(name, existing_type=column_type, nullable=False)
