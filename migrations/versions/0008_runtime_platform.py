"""Register one immutable platform identity per independently operated database."""

import sqlalchemy as sa
from alembic import op

# 归属表只增加元数据，不搬迁或删除任何历史业务记录。
revision = "0008_runtime_platform"
down_revision = "0007_taobao_repeated_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a singleton platform identity claimed by validated runtime entrypoints."""
    op.create_table(
        "runtime_platform",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_runtime_platform_singleton"),
        sa.CheckConstraint("platform IN ('compass', 'taobao')", name="ck_runtime_platform_name"),
    )


def downgrade() -> None:
    """Remove only runtime identity metadata while retaining business history."""
    op.drop_table("runtime_platform")
