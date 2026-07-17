"""Add immutable request-filter snapshots to collection batches.

Revision ID: 0002_batch_filter_snapshot
Revises: 0001_initial
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# 当前迁移为既有开发数据库增加批次筛选快照。
revision: str = "0002_batch_filter_snapshot"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable snapshots without inventing values for historical batches."""

    # 旧批次和 Scheduler-only 记录保持空值，新采集批次由应用层写入。
    op.add_column(
        "collection_batches",
        sa.Column("brand_type", sa.Integer(), nullable=True),
    )
    op.add_column(
        "collection_batches",
        sa.Column("price_bin", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    """Remove request-filter snapshots."""

    # 按创建逆序移除列，保持降级操作可预测。
    op.drop_column("collection_batches", "price_bin")
    op.drop_column("collection_batches", "brand_type")
