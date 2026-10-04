"""Frozen PostgreSQL baseline; historical SQLite revisions remain archived separately."""

from alembic import op

# 独立版本链只用于新 PostgreSQL 数据库，不导入或升级旧 SQLite。
revision = "pg0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# SQL 固定于此基线，后续模型变化必须通过新增迁移表达。
STATEMENTS = (
    """CREATE TABLE collection_batches (
	platform VARCHAR(64) NOT NULL,
	config_snapshot JSON,
	id VARCHAR(32) NOT NULL,
	task_id VARCHAR(120) NOT NULL,
	business_date DATE NOT NULL,
	planned_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	mode VARCHAR(16) NOT NULL,
	status VARCHAR(32) NOT NULL,
	version INTEGER,
	brand_type INTEGER,
	price_bin VARCHAR(120),
	root_category_id VARCHAR(128),
	root_category_name VARCHAR(512),
	manifest_path VARCHAR(1024),
	category_tree_raw_path VARCHAR(1024),
	csv_path VARCHAR(1024),
	discovered_category_count INTEGER NOT NULL,
	successful_category_count INTEGER NOT NULL,
	failed_category_count INTEGER NOT NULL,
	not_started_category_count INTEGER NOT NULL,
	saved_page_count INTEGER NOT NULL,
	collected_item_count INTEGER NOT NULL,
	error_category VARCHAR(120),
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	published_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_collection_batches_mode CHECK (mode IN ('normal', 'dry_run', 'force')),
	CONSTRAINT ck_collection_batches_status CHECK (status IN ('running', 'publishing', 'success', 'partial_success', 'failed', 'auth_required', 'interrupted', 'abandoned', 'missed', 'skipped_busy')),
	CONSTRAINT ck_collection_batches_version CHECK (version IS NULL OR version >= 1),
	CONSTRAINT ck_collection_batches_nonnegative_counts CHECK (discovered_category_count >= 0 AND successful_category_count >= 0 AND failed_category_count >= 0 AND not_started_category_count >= 0 AND saved_page_count >= 0 AND collected_item_count >= 0),
	CONSTRAINT ck_collection_batches_category_counts CHECK (successful_category_count + failed_category_count + not_started_category_count <= discovered_category_count),
	CONSTRAINT ck_collection_batches_publication_counts CHECK (((status = 'success' AND discovered_category_count > 0 AND successful_category_count = discovered_category_count AND failed_category_count = 0 AND not_started_category_count = 0) OR (status = 'partial_success' AND discovered_category_count > 0 AND successful_category_count > 0 AND failed_category_count >= 1 AND not_started_category_count = 0 AND successful_category_count + failed_category_count = discovered_category_count) OR status NOT IN ('success', 'partial_success'))),
	CONSTRAINT ck_collection_batches_lifecycle_time CHECK (((status IN ('running', 'publishing') AND finished_at IS NULL) OR (status NOT IN ('running', 'publishing') AND finished_at IS NOT NULL))),
	CONSTRAINT ck_collection_batches_terminal_error CHECK ((status NOT IN ('failed', 'auth_required', 'interrupted', 'abandoned', 'missed', 'skipped_busy') OR error_category IS NOT NULL)),
	CONSTRAINT ck_collection_batches_publishing CHECK (((status = 'publishing' AND mode IN ('normal', 'force') AND version IS NOT NULL AND csv_path IS NOT NULL AND published_at IS NULL) OR status <> 'publishing')),
	CONSTRAINT ck_collection_batches_success_publication CHECK (((status IN ('success', 'partial_success') AND mode = 'dry_run' AND version IS NULL AND csv_path IS NULL AND published_at IS NULL) OR (status IN ('success', 'partial_success') AND mode IN ('normal', 'force') AND version IS NOT NULL AND csv_path IS NOT NULL AND published_at IS NOT NULL) OR status NOT IN ('success', 'partial_success'))),
	CONSTRAINT ck_collection_batches_unpublished_states CHECK ((status IN ('publishing', 'success', 'partial_success') OR (version IS NULL AND csv_path IS NULL AND published_at IS NULL))),
	CONSTRAINT ck_collection_batches_scheduler_only CHECK ((status NOT IN ('missed', 'skipped_busy') OR (mode = 'normal' AND manifest_path IS NULL AND category_tree_raw_path IS NULL AND discovered_category_count = 0 AND successful_category_count = 0 AND failed_category_count = 0 AND not_started_category_count = 0 AND saved_page_count = 0 AND collected_item_count = 0))),
	CONSTRAINT uq_collection_batch_version UNIQUE (task_id, planned_at, version)
)""",
    """CREATE INDEX ix_collection_batches_planned_at ON collection_batches (planned_at)""",
    """CREATE INDEX ix_collection_batches_published_at ON collection_batches (published_at)""",
    """CREATE INDEX ix_collection_batches_started_at ON collection_batches (started_at)""",
    """CREATE INDEX ix_collection_batches_task_id ON collection_batches (task_id)""",
    """CREATE TABLE runtime_platform (
	id SERIAL NOT NULL,
	platform VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_runtime_platform_singleton CHECK (id = 1),
	CONSTRAINT ck_runtime_platform_name CHECK (platform IN ('compass', 'taobao'))
)""",
    """CREATE TABLE scheduler_checkpoints (
	task_id VARCHAR(120) NOT NULL,
	last_checked_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (task_id)
)""",
    """CREATE TABLE category_runs (
	scope_path JSON NOT NULL,
	platform_metadata JSON NOT NULL,
	id VARCHAR(32) NOT NULL,
	batch_id VARCHAR(32) NOT NULL,
	discovery_order INTEGER NOT NULL,
	level1_category_id VARCHAR(128) NOT NULL,
	level1_category_name VARCHAR(512) NOT NULL,
	level2_category_id VARCHAR(128) NOT NULL,
	level2_category_name VARCHAR(512) NOT NULL,
	category_id VARCHAR(128) NOT NULL,
	category_name VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	api_total INTEGER,
	target_page_count INTEGER,
	saved_page_count INTEGER NOT NULL,
	saved_item_count INTEGER NOT NULL,
	failed_page INTEGER,
	error_category VARCHAR(120),
	started_at TIMESTAMP WITHOUT TIME ZONE,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_category_runs_discovery_order CHECK (discovery_order >= 1),
	CONSTRAINT ck_category_runs_status CHECK (status IN ('pending', 'running', 'success', 'failed', 'not_started', 'interrupted', 'abandoned')),
	CONSTRAINT ck_category_runs_counts CHECK ((api_total IS NULL OR api_total >= 0) AND (target_page_count IS NULL OR target_page_count >= 1) AND saved_page_count >= 0 AND saved_item_count >= 0 AND (failed_page IS NULL OR failed_page >= 1)),
	CONSTRAINT ck_category_runs_lifecycle_time CHECK (((status = 'pending' AND started_at IS NULL AND finished_at IS NULL AND saved_page_count = 0 AND saved_item_count = 0) OR (status = 'not_started' AND started_at IS NULL AND finished_at IS NOT NULL AND saved_page_count = 0 AND saved_item_count = 0) OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR (status IN ('success', 'failed', 'interrupted', 'abandoned') AND started_at IS NOT NULL AND finished_at IS NOT NULL))),
	CONSTRAINT ck_category_runs_success CHECK ((status <> 'success' OR (api_total IS NOT NULL AND target_page_count IS NOT NULL AND saved_page_count = target_page_count AND saved_item_count = api_total AND failed_page IS NULL AND error_category IS NULL))),
	CONSTRAINT ck_category_runs_terminal_error CHECK ((status NOT IN ('failed', 'interrupted', 'abandoned') OR error_category IS NOT NULL)),
	CONSTRAINT uq_category_run_category UNIQUE (batch_id, category_id),
	CONSTRAINT uq_category_run_discovery_order UNIQUE (batch_id, discovery_order),
	FOREIGN KEY(batch_id) REFERENCES collection_batches (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_category_runs_batch_id ON category_runs (batch_id)""",
    """CREATE INDEX ix_category_runs_status ON category_runs (status)""",
    """CREATE TABLE product_rank_entries (
	id SERIAL NOT NULL,
	platform VARCHAR(32) DEFAULT 'compass' NOT NULL,
	category_run_id VARCHAR(32) NOT NULL,
	captured_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	page_no INTEGER NOT NULL,
	rank INTEGER NOT NULL,
	product_id VARCHAR(128) NOT NULL,
	product_name VARCHAR(2048) NOT NULL,
	image_url VARCHAR(4096),
	newly_on_ranking BOOLEAN,
	pay_amount_min_value NUMERIC(24, 4),
	pay_amount_max_value NUMERIC(24, 4),
	pay_amount_unit VARCHAR(32),
	pay_combo_count_min_value NUMERIC(24, 4),
	pay_combo_count_max_value NUMERIC(24, 4),
	pay_combo_count_unit VARCHAR(32),
	product_url VARCHAR(4096),
	pay_buyer_count_raw VARCHAR(128),
	pay_buyer_count_min_value NUMERIC(24, 4),
	pay_buyer_count_max_value NUMERIC(24, 4),
	pay_buyer_count_unit VARCHAR(32),
	visitor_count_raw VARCHAR(128),
	visitor_count_min_value NUMERIC(24, 4),
	visitor_count_max_value NUMERIC(24, 4),
	visitor_count_unit VARCHAR(32),
	PRIMARY KEY (id),
	CONSTRAINT ck_product_rank_entries_page_no CHECK (page_no >= 1),
	CONSTRAINT ck_product_rank_entries_rank CHECK (rank >= 1),
	CONSTRAINT ck_product_rank_entries_pay_amount CHECK (pay_amount_min_value >= 0 AND pay_amount_max_value >= 0 AND pay_amount_min_value <= pay_amount_max_value),
	CONSTRAINT ck_product_rank_entries_pay_combo_count CHECK (pay_combo_count_min_value >= 0 AND pay_combo_count_max_value >= 0 AND pay_combo_count_min_value <= pay_combo_count_max_value),
	CONSTRAINT ck_product_rank_entries_pay_buyer_count CHECK ((pay_buyer_count_min_value IS NULL AND pay_buyer_count_max_value IS NULL AND pay_buyer_count_unit IS NULL) OR (pay_buyer_count_min_value IS NOT NULL AND pay_buyer_count_max_value IS NOT NULL AND pay_buyer_count_unit IS NOT NULL AND pay_buyer_count_unit = 'count' AND pay_buyer_count_min_value >= 0 AND pay_buyer_count_min_value <= pay_buyer_count_max_value)),
	CONSTRAINT ck_product_rank_entries_visitor_count CHECK ((visitor_count_min_value IS NULL AND visitor_count_max_value IS NULL AND visitor_count_unit IS NULL) OR (visitor_count_min_value IS NOT NULL AND visitor_count_max_value IS NOT NULL AND visitor_count_unit IS NOT NULL AND visitor_count_unit = 'count' AND visitor_count_min_value >= 0 AND visitor_count_min_value <= visitor_count_max_value)),
	CONSTRAINT ck_product_rank_entries_platform CHECK (platform IN ('compass', 'taobao')),
	CONSTRAINT uq_category_run_rank UNIQUE (category_run_id, rank),
	FOREIGN KEY(category_run_id) REFERENCES category_runs (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_product_rank_entries_category_run_id ON product_rank_entries (category_run_id)""",
    """CREATE UNIQUE INDEX uq_category_run_compass_product ON product_rank_entries (category_run_id, product_id) WHERE platform = 'compass'""",
    """CREATE TABLE raw_responses (
	id SERIAL NOT NULL,
	category_run_id VARCHAR(32) NOT NULL,
	page_no INTEGER NOT NULL,
	path VARCHAR(1024) NOT NULL,
	item_count INTEGER NOT NULL,
	captured_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	safe_params JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_raw_responses_page_no CHECK (page_no >= 1),
	CONSTRAINT ck_raw_responses_item_count CHECK (item_count >= 0),
	CONSTRAINT uq_raw_response_page UNIQUE (category_run_id, page_no),
	FOREIGN KEY(category_run_id) REFERENCES category_runs (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_raw_responses_category_run_id ON raw_responses (category_run_id)""",
    """CREATE TABLE product_rank_entry_shops (
	id SERIAL NOT NULL,
	entry_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	shop_id VARCHAR(128),
	shop_url VARCHAR(4096),
	seller_user_id VARCHAR(128),
	shop_name VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_product_rank_entry_shops_position CHECK (position >= 0),
	CONSTRAINT uq_entry_shop_position UNIQUE (entry_id, position),
	FOREIGN KEY(entry_id) REFERENCES product_rank_entries (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_product_rank_entry_shops_entry_id ON product_rank_entry_shops (entry_id)""",
)


def upgrade() -> None:
    """Create the frozen schema using native PostgreSQL constraints and identities."""
    for statement in STATEMENTS:
        # 所有语句来自版本内固定定义，不包含配置或凭证。
        op.execute(statement)

def downgrade() -> None:
    """Remove baseline tables in reverse foreign-key dependency order."""
    op.drop_table('product_rank_entry_shops')
    op.drop_table('raw_responses')
    op.drop_table('product_rank_entries')
    op.drop_table('category_runs')
    op.drop_table('scheduler_checkpoints')
    op.drop_table('runtime_platform')
    op.drop_table('collection_batches')
