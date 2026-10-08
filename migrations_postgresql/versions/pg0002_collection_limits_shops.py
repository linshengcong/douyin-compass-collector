"""固定采集计划与店铺展示字段；不回填历史商品。"""
from alembic import op

# PostgreSQL 独立版本链保持已有基线不可变。
revision = "pg0002_limits_shops"
down_revision = "pg0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    """允许正常限页成功，历史 null 计划继续按 api_total 校验。"""
    op.execute("ALTER TABLE category_runs ADD COLUMN planned_item_count INTEGER CHECK (planned_item_count >= 0 AND planned_item_count <= api_total)")
    op.execute("ALTER TABLE category_runs DROP CONSTRAINT ck_category_runs_success")
    op.execute("ALTER TABLE category_runs ADD CONSTRAINT ck_category_runs_success CHECK (status <> 'success' OR (api_total IS NOT NULL AND target_page_count IS NOT NULL AND saved_page_count = target_page_count AND saved_item_count = COALESCE(planned_item_count, api_total) AND failed_page IS NULL AND error_category IS NULL))")
    op.execute("ALTER TABLE product_rank_entry_shops ADD COLUMN image_url VARCHAR(4096), ADD COLUMN is_tmall BOOLEAN")


def downgrade():
    """限页成功记录不能降级为旧完整采集契约，阻止破坏历史证据。"""
    raise RuntimeError("collection limit migration is forward-only")
