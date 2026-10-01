"""Platform identity, immutable task snapshots and normalized metric units."""

import json
import sqlalchemy as sa
from alembic import op

# 保留完整历史迁移链，不通过重建空库替代数据迁移。
revision = "0005_platform_capture"
down_revision = "0004_product_image_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill known historical values without inventing requested scopes."""
    op.add_column(
        "raw_responses",
        sa.Column("safe_params", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "collection_batches",
        sa.Column("platform", sa.String(64), nullable=False, server_default="compass"),
    )
    op.add_column(
        "collection_batches", sa.Column("config_snapshot", sa.JSON(), nullable=True)
    )
    op.add_column(
        "category_runs",
        sa.Column("scope_path", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "category_runs",
        sa.Column("platform_metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    # 历史过滤值是真实批次快照；历史请求范围未知，不能使用新任务配置。
    connection = op.get_bind()
    for row in connection.execute(
        sa.text("SELECT id,brand_type,price_bin FROM collection_batches")
    ).mappings():
        snapshot = {
            "platform": "compass",
            "requested_selection": None,
            "filters": {"brand_type": row["brand_type"], "price_bin": row["price_bin"]},
        }
        connection.execute(
            sa.text(
                "UPDATE collection_batches SET config_snapshot=:snapshot WHERE id=:id"
            ),
            {"snapshot": json.dumps(snapshot), "id": row["id"]},
        )
    for row in connection.execute(
        sa.text(
            "SELECT id,level1_category_id,level2_category_id,category_id,level1_category_name,level2_category_name,category_name FROM category_runs"
        )
    ).mappings():
        # 有序路径取自已保存分类快照，名称及 ID 不受当前平台分类树影响。
        path = [
            row["level1_category_name"],
            row["level2_category_name"],
            row["category_name"],
        ]
        metadata = {
            "industry_id": row["level1_category_id"],
            "level2_id": row["level2_category_id"],
            "category_id": row["category_id"],
        }
        connection.execute(
            sa.text(
                "UPDATE category_runs SET scope_path=:path,platform_metadata=:metadata WHERE id=:id"
            ),
            {
                "path": json.dumps(path, ensure_ascii=False),
                "metadata": json.dumps(metadata),
                "id": row["id"],
            },
        )
    # SQLite 允许数值列保存小数；先转换一次旧单位，再声明规范化精度。
    connection.execute(
        sa.text(
            "UPDATE product_rank_entries SET pay_amount_min_value=pay_amount_min_value/100.0,pay_amount_max_value=pay_amount_max_value/100.0,pay_amount_unit='CNY' WHERE pay_amount_unit='price'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE product_rank_entries SET pay_combo_count_min_value=pay_combo_count_min_value/10.0,pay_combo_count_max_value=pay_combo_count_max_value/10.0,pay_combo_count_unit='count' WHERE pay_combo_count_unit='number'"
        )
    )
    with op.batch_alter_table("product_rank_entries") as batch:
        for name in (
            "pay_amount_min_value",
            "pay_amount_max_value",
            "pay_combo_count_min_value",
            "pay_combo_count_max_value",
        ):
            batch.alter_column(
                name,
                existing_type=sa.BigInteger(),
                type_=sa.Numeric(24, 4),
                existing_nullable=False,
            )


def downgrade() -> None:
    """Restore legacy units only for an explicitly requested downgrade."""
    # 使用固定单位反向还原，原始响应及其他审计材料不变。
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE product_rank_entries SET pay_amount_min_value=ROUND(pay_amount_min_value*100),pay_amount_max_value=ROUND(pay_amount_max_value*100),pay_amount_unit='price' WHERE pay_amount_unit='CNY'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE product_rank_entries SET pay_combo_count_min_value=ROUND(pay_combo_count_min_value*10),pay_combo_count_max_value=ROUND(pay_combo_count_max_value*10),pay_combo_count_unit='number' WHERE pay_combo_count_unit='count'"
        )
    )
    with op.batch_alter_table("product_rank_entries") as batch:
        for name in (
            "pay_amount_min_value",
            "pay_amount_max_value",
            "pay_combo_count_min_value",
            "pay_combo_count_max_value",
        ):
            batch.alter_column(
                name,
                existing_type=sa.Numeric(24, 4),
                type_=sa.BigInteger(),
                existing_nullable=False,
            )
    op.drop_column("category_runs", "platform_metadata")
    op.drop_column("category_runs", "scope_path")
    op.drop_column("collection_batches", "config_snapshot")
    op.drop_column("collection_batches", "platform")
    op.drop_column("raw_responses", "safe_params")
