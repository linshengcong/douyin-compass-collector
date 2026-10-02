"""Allow repeated Taobao item IDs while preserving each distinct source rank."""

import sqlalchemy as sa
from alembic import op

# 新规则独立迁移，历史商品及店铺外键保留原身份。
revision = "0007_taobao_repeated_items"
down_revision = "0006_taobao_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Use explicit source platforms to scope product uniqueness to Compass."""
    with op.batch_alter_table("product_rank_entries") as batch:
        batch.add_column(sa.Column("platform", sa.String(32), nullable=False, server_default="compass"))
        batch.drop_constraint("uq_category_run_product", type_="unique")
        batch.create_check_constraint("ck_product_rank_entries_platform", "platform IN ('compass', 'taobao')")
    # 从实际父批次回填历史平台，不能将已有淘宝记录标为罗盘。
    op.execute(sa.text("UPDATE product_rank_entries SET platform = "
                       "(SELECT collection_batches.platform FROM category_runs "
                       "JOIN collection_batches ON collection_batches.id = category_runs.batch_id "
                       "WHERE category_runs.id = product_rank_entries.category_run_id)"))
    op.create_index("uq_category_run_compass_product", "product_rank_entries",
                    ["category_run_id", "product_id"], unique=True,
                    sqlite_where=sa.text("platform = 'compass'"))


def downgrade() -> None:
    """Refuse to erase repeated ranking positions to restore old uniqueness."""
    # 存在新规则允许的重复时必须先导出处理，降级不能删除源排名记录。
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM (SELECT category_run_id, product_id "
                                  "FROM product_rank_entries GROUP BY category_run_id, product_id "
                                  "HAVING COUNT(*) > 1)")):
        raise RuntimeError("cannot downgrade while repeated Taobao ranking items exist")
    op.drop_index("uq_category_run_compass_product", table_name="product_rank_entries")
    with op.batch_alter_table("product_rank_entries") as batch:
        batch.drop_constraint("ck_product_rank_entries_platform", type_="check")
        batch.drop_column("platform")
        batch.create_unique_constraint("uq_category_run_product", ["category_run_id", "product_id"])
