"""Allow partial publication after any number of category-level failures.

Revision ID: 0003_unbounded_partial_success_failures
Revises: 0002_batch_filter_snapshot
"""

from typing import Sequence

from alembic import op


# 此迁移扩展 partial_success 的失败分类计数，不改变已存在批次数据。
revision: str = "0003_unbounded_partial_success_failures"
down_revision: str | None = "0002_batch_filter_snapshot"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Replace the SQLite publication-count constraint with an unbounded variant."""

    # SQLite 通过 batch_alter_table 重建表，保留现有运行数据和其他约束。
    with op.batch_alter_table("collection_batches") as batch_op:
        batch_op.drop_constraint(
            "ck_collection_batches_publication_counts",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_collection_batches_publication_counts",
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
        )


def downgrade() -> None:
    """Restore the previous two-failure partial-publication limit."""

    # 降级仅恢复约束；若已有三条以上失败的 partial_success 批次，SQLite 会拒绝降级。
    with op.batch_alter_table("collection_batches") as batch_op:
        batch_op.drop_constraint(
            "ck_collection_batches_publication_counts",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_collection_batches_publication_counts",
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
        )
