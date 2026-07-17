"""Create the clean dynamic-category collection baseline.

Revision ID: 0001_initial
Revises: None
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# 新基线从 v1 开始，不携带旧单分类数据模型。
revision: str = "0001_initial"
# 开发期已清理旧库，因此没有上游迁移。
down_revision: str | None = None
# 当前迁移不属于任何分支。
branch_labels: Sequence[str] | None = None
# 当前迁移没有附加依赖。
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create batch, category-run, ranking, raw, shop, and scheduler tables."""

    op.create_table(
        "collection_batches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=120), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("planned_at", sa.DateTime(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("root_category_id", sa.String(length=128), nullable=True),
        sa.Column("root_category_name", sa.String(length=512), nullable=True),
        sa.Column("manifest_path", sa.String(length=1024), nullable=True),
        sa.Column("category_tree_raw_path", sa.String(length=1024), nullable=True),
        sa.Column("csv_path", sa.String(length=1024), nullable=True),
        sa.Column(
            "discovered_category_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "successful_category_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "failed_category_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "not_started_category_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "saved_page_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "collected_item_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_category", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('normal', 'dry_run', 'force')",
            name="ck_collection_batches_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'publishing', 'success', 'partial_success', "
            "'failed', 'auth_required', 'interrupted', 'abandoned', 'missed', "
            "'skipped_busy')",
            name="ck_collection_batches_status",
        ),
        sa.CheckConstraint(
            "version IS NULL OR version >= 1",
            name="ck_collection_batches_version",
        ),
        sa.CheckConstraint(
            "discovered_category_count >= 0 AND successful_category_count >= 0 "
            "AND failed_category_count >= 0 AND not_started_category_count >= 0 "
            "AND saved_page_count >= 0 AND collected_item_count >= 0",
            name="ck_collection_batches_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "successful_category_count + failed_category_count + "
            "not_started_category_count <= discovered_category_count",
            name="ck_collection_batches_category_counts",
        ),
        sa.CheckConstraint(
            "((status = 'success' AND discovered_category_count > 0 "
            "AND successful_category_count = discovered_category_count "
            "AND failed_category_count = 0 AND not_started_category_count = 0) OR "
            "(status = 'partial_success' AND discovered_category_count > 0 "
            "AND successful_category_count > 0 "
            "AND failed_category_count BETWEEN 1 AND 2 "
            "AND not_started_category_count = 0 "
            "AND successful_category_count + failed_category_count = "
            "discovered_category_count) OR "
            "status NOT IN ('success', 'partial_success'))",
            name="ck_collection_batches_publication_counts",
        ),
        sa.CheckConstraint(
            "((status IN ('running', 'publishing') AND finished_at IS NULL) OR "
            "(status NOT IN ('running', 'publishing') AND finished_at IS NOT NULL))",
            name="ck_collection_batches_lifecycle_time",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('failed', 'auth_required', 'interrupted', 'abandoned', "
            "'missed', 'skipped_busy') OR error_category IS NOT NULL)",
            name="ck_collection_batches_terminal_error",
        ),
        sa.CheckConstraint(
            "((status = 'publishing' AND mode IN ('normal', 'force') "
            "AND version IS NOT NULL AND csv_path IS NOT NULL "
            "AND published_at IS NULL) OR status <> 'publishing')",
            name="ck_collection_batches_publishing",
        ),
        sa.CheckConstraint(
            "((status IN ('success', 'partial_success') AND mode = 'dry_run' "
            "AND version IS NULL AND csv_path IS NULL AND published_at IS NULL) OR "
            "(status IN ('success', 'partial_success') AND mode IN ('normal', 'force') "
            "AND version IS NOT NULL AND csv_path IS NOT NULL "
            "AND published_at IS NOT NULL) OR "
            "status NOT IN ('success', 'partial_success'))",
            name="ck_collection_batches_success_publication",
        ),
        sa.CheckConstraint(
            "(status IN ('publishing', 'success', 'partial_success') OR "
            "(version IS NULL AND csv_path IS NULL AND published_at IS NULL))",
            name="ck_collection_batches_unpublished_states",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('missed', 'skipped_busy') OR "
            "(mode = 'normal' AND manifest_path IS NULL "
            "AND category_tree_raw_path IS NULL "
            "AND discovered_category_count = 0 AND successful_category_count = 0 "
            "AND failed_category_count = 0 AND not_started_category_count = 0 "
            "AND saved_page_count = 0 AND collected_item_count = 0))",
            name="ck_collection_batches_scheduler_only",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "planned_at",
            "version",
            name="uq_collection_batch_version",
        ),
    )
    op.create_index(
        "ix_collection_batches_task_id",
        "collection_batches",
        ["task_id"],
    )
    op.create_index(
        "ix_collection_batches_planned_at",
        "collection_batches",
        ["planned_at"],
    )
    op.create_index(
        "ix_collection_batches_started_at",
        "collection_batches",
        ["started_at"],
    )
    op.create_index(
        "ix_collection_batches_published_at",
        "collection_batches",
        ["published_at"],
    )

    op.create_table(
        "category_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column("discovery_order", sa.Integer(), nullable=False),
        sa.Column("level1_category_id", sa.String(length=128), nullable=False),
        sa.Column("level1_category_name", sa.String(length=512), nullable=False),
        sa.Column("level2_category_id", sa.String(length=128), nullable=False),
        sa.Column("level2_category_name", sa.String(length=512), nullable=False),
        sa.Column("category_id", sa.String(length=128), nullable=False),
        sa.Column("category_name", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("api_total", sa.Integer(), nullable=True),
        sa.Column("target_page_count", sa.Integer(), nullable=True),
        sa.Column("saved_page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saved_item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_page", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "discovery_order >= 1",
            name="ck_category_runs_discovery_order",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'not_started', "
            "'interrupted', 'abandoned')",
            name="ck_category_runs_status",
        ),
        sa.CheckConstraint(
            "(api_total IS NULL OR api_total >= 0) "
            "AND (target_page_count IS NULL OR target_page_count >= 1) "
            "AND saved_page_count >= 0 AND saved_item_count >= 0 "
            "AND (failed_page IS NULL OR failed_page >= 1)",
            name="ck_category_runs_counts",
        ),
        sa.CheckConstraint(
            "((status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND saved_page_count = 0 AND saved_item_count = 0) OR "
            "(status = 'not_started' AND started_at IS NULL AND finished_at IS NOT NULL "
            "AND saved_page_count = 0 AND saved_item_count = 0) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('success', 'failed', 'interrupted', 'abandoned') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL))",
            name="ck_category_runs_lifecycle_time",
        ),
        sa.CheckConstraint(
            "(status <> 'success' OR (api_total IS NOT NULL "
            "AND target_page_count IS NOT NULL "
            "AND saved_page_count = target_page_count "
            "AND saved_item_count = api_total "
            "AND failed_page IS NULL AND error_category IS NULL))",
            name="ck_category_runs_success",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('failed', 'interrupted', 'abandoned') "
            "OR error_category IS NOT NULL)",
            name="ck_category_runs_terminal_error",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["collection_batches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "category_id",
            name="uq_category_run_category",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "discovery_order",
            name="uq_category_run_discovery_order",
        ),
    )
    op.create_index("ix_category_runs_batch_id", "category_runs", ["batch_id"])
    op.create_index("ix_category_runs_status", "category_runs", ["status"])

    op.create_table(
        "raw_responses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_run_id", sa.String(length=32), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("page_no >= 1", name="ck_raw_responses_page_no"),
        sa.CheckConstraint("item_count >= 0", name="ck_raw_responses_item_count"),
        sa.ForeignKeyConstraint(
            ["category_run_id"],
            ["category_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "category_run_id",
            "page_no",
            name="uq_raw_response_page",
        ),
    )
    op.create_index(
        "ix_raw_responses_category_run_id",
        "raw_responses",
        ["category_run_id"],
    )

    op.create_table(
        "product_rank_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_run_id", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint("page_no >= 1", name="ck_product_rank_entries_page_no"),
        sa.CheckConstraint("rank >= 1", name="ck_product_rank_entries_rank"),
        sa.CheckConstraint(
            "pay_amount_min_value >= 0 AND pay_amount_max_value >= 0 "
            "AND pay_amount_min_value <= pay_amount_max_value",
            name="ck_product_rank_entries_pay_amount",
        ),
        sa.CheckConstraint(
            "pay_combo_count_min_value >= 0 AND pay_combo_count_max_value >= 0 "
            "AND pay_combo_count_min_value <= pay_combo_count_max_value",
            name="ck_product_rank_entries_pay_combo_count",
        ),
        sa.ForeignKeyConstraint(
            ["category_run_id"],
            ["category_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "category_run_id",
            "product_id",
            name="uq_category_run_product",
        ),
        sa.UniqueConstraint(
            "category_run_id",
            "rank",
            name="uq_category_run_rank",
        ),
    )
    op.create_index(
        "ix_product_rank_entries_category_run_id",
        "product_rank_entries",
        ["category_run_id"],
    )

    op.create_table(
        "product_rank_entry_shops",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.String(length=128), nullable=False),
        sa.Column("shop_name", sa.String(length=1024), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_product_rank_entry_shops_position"),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["product_rank_entries.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_id",
            "position",
            name="uq_entry_shop_position",
        ),
    )
    op.create_index(
        "ix_product_rank_entry_shops_entry_id",
        "product_rank_entry_shops",
        ["entry_id"],
    )

    op.create_table(
        "scheduler_checkpoints",
        sa.Column("task_id", sa.String(length=120), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    """Drop the clean baseline in reverse dependency order."""

    op.drop_table("scheduler_checkpoints")
    op.drop_index(
        "ix_product_rank_entry_shops_entry_id",
        table_name="product_rank_entry_shops",
    )
    op.drop_table("product_rank_entry_shops")
    op.drop_index(
        "ix_product_rank_entries_category_run_id",
        table_name="product_rank_entries",
    )
    op.drop_table("product_rank_entries")
    op.drop_index(
        "ix_raw_responses_category_run_id",
        table_name="raw_responses",
    )
    op.drop_table("raw_responses")
    op.drop_index("ix_category_runs_status", table_name="category_runs")
    op.drop_index("ix_category_runs_batch_id", table_name="category_runs")
    op.drop_table("category_runs")
    op.drop_index("ix_collection_batches_published_at", table_name="collection_batches")
    op.drop_index("ix_collection_batches_started_at", table_name="collection_batches")
    op.drop_index("ix_collection_batches_planned_at", table_name="collection_batches")
    op.drop_index("ix_collection_batches_task_id", table_name="collection_batches")
    op.drop_table("collection_batches")
