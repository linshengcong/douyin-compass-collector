"""Create stage-two collection and product ranking tables.

Revision ID: 0001_initial
Revises: None
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识是已发布数据库契约的一部分。
revision: str = "0001_initial"
# 首个迁移没有上游版本。
down_revision: str | None = None
# 首个迁移不属于任何分支。
branch_labels: Sequence[str] | None = None
# 首个迁移没有附加依赖。
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create all stage-two tables, constraints, and indexes."""

    op.create_table(
        "collection_batches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=120), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("planned_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("csv_path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "planned_at", "version", name="uq_batch_version"),
    )
    op.create_index("ix_collection_batches_task_id", "collection_batches", ["task_id"])
    op.create_index("ix_collection_batches_planned_at", "collection_batches", ["planned_at"])
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("task_id", sa.String(length=120), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("planned_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("error_category", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["collection_batches.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collection_runs_batch_id", "collection_runs", ["batch_id"])
    op.create_index("ix_collection_runs_task_id", "collection_runs", ["task_id"])
    op.create_index("ix_collection_runs_planned_at", "collection_runs", ["planned_at"])
    op.create_table(
        "raw_responses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "page_no", name="uq_raw_response_page"),
    )
    op.create_index("ix_raw_responses_run_id", "raw_responses", ["run_id"])
    op.create_table(
        "product_rank_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=120), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("planned_at", sa.DateTime(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.String(length=128), nullable=False),
        sa.Column("product_name", sa.String(length=2048), nullable=False),
        sa.Column("newly_on_ranking", sa.Boolean(), nullable=False),
        sa.Column("pay_amount_min_value", sa.BigInteger(), nullable=False),
        sa.Column("pay_amount_max_value", sa.BigInteger(), nullable=False),
        sa.Column("pay_amount_unit", sa.String(length=32), nullable=False),
        sa.Column("pay_combo_count_min_value", sa.BigInteger(), nullable=False),
        sa.Column("pay_combo_count_max_value", sa.BigInteger(), nullable=False),
        sa.Column("pay_combo_count_unit", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["collection_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "product_id", name="uq_batch_product"),
        sa.UniqueConstraint("batch_id", "rank", name="uq_batch_rank"),
    )
    op.create_index("ix_product_rank_entries_batch_id", "product_rank_entries", ["batch_id"])
    op.create_index("ix_product_rank_entries_run_id", "product_rank_entries", ["run_id"])
    op.create_index("ix_product_rank_entries_task_id", "product_rank_entries", ["task_id"])
    op.create_table(
        "product_rank_entry_shops",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.String(length=128), nullable=False),
        sa.Column("shop_name", sa.String(length=1024), nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["product_rank_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "position", name="uq_entry_shop_position"),
    )
    op.create_index("ix_product_rank_entry_shops_entry_id", "product_rank_entry_shops", ["entry_id"])


def downgrade() -> None:
    """Drop stage-two tables in reverse dependency order."""

    op.drop_index("ix_product_rank_entry_shops_entry_id", table_name="product_rank_entry_shops")
    op.drop_table("product_rank_entry_shops")
    op.drop_index("ix_product_rank_entries_task_id", table_name="product_rank_entries")
    op.drop_index("ix_product_rank_entries_run_id", table_name="product_rank_entries")
    op.drop_index("ix_product_rank_entries_batch_id", table_name="product_rank_entries")
    op.drop_table("product_rank_entries")
    op.drop_index("ix_raw_responses_run_id", table_name="raw_responses")
    op.drop_table("raw_responses")
    op.drop_index("ix_collection_runs_planned_at", table_name="collection_runs")
    op.drop_index("ix_collection_runs_task_id", table_name="collection_runs")
    op.drop_index("ix_collection_runs_batch_id", table_name="collection_runs")
    op.drop_table("collection_runs")
    op.drop_index("ix_collection_batches_planned_at", table_name="collection_batches")
    op.drop_index("ix_collection_batches_task_id", table_name="collection_batches")
    op.drop_table("collection_batches")
