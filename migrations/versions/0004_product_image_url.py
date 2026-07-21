"""Persist optional public product image URLs for website thumbnails.

Revision ID: 0004_product_image_url
Revises: 0003_unbounded_partial_success_failures
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


# 图片地址必须允许为空，才能在不重采历史批次的情况下升级现有数据库。
revision: str = "0004_product_image_url"
down_revision: str | None = "0003_unbounded_partial_success_failures"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable image URL that source responses may provide per product."""

    op.add_column(
        "product_rank_entries",
        sa.Column("image_url", sa.String(length=4096), nullable=True),
    )


def downgrade() -> None:
    """Remove the product image URL column during a deliberate schema rollback."""

    op.drop_column("product_rank_entries", "image_url")
