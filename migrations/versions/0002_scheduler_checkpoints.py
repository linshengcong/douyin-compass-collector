"""Add durable Scheduler reconciliation checkpoints.

Revision ID: 0002_scheduler_checkpoints
Revises: 0001_initial
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识是已发布数据库契约的一部分。
revision: str = "0002_scheduler_checkpoints"
# 检查点迁移建立在阶段二初始表之上。
down_revision: str | None = "0001_initial"
# 当前迁移不属于任何分支。
branch_labels: Sequence[str] | None = None
# 当前迁移没有附加依赖。
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create one durable reconciliation checkpoint per task."""

    op.create_table(
        "scheduler_checkpoints",
        sa.Column("task_id", sa.String(length=120), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    """Remove Scheduler checkpoints without touching collection history."""

    op.drop_table("scheduler_checkpoints")
