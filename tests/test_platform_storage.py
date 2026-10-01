"""Historical migration, platform publication and legacy retention regressions."""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from alembic import command
from alembic.config import Config
from compass_collector.persistence import Database, upgrade_database
from compass_collector.exporter import format_metric_range
from compass_collector.models import MetricRange
from compass_collector.retention import cleanup_runtime
from compass_collector.config import load_config
from compass_collector.web_publisher import WebPublisher, WebPublicationSettings
from test_stage_four_persistence import prepare_collected_batch, prepare_staged_csv


def test_migration_backups_preserve_history_relationships_and_display(tmp_path):
    """Back up a populated old revision, then preserve counts and display units."""
    # Build a real published batch before reconstructing the legacy revision.
    database, batch = prepare_collected_batch(
        tmp_path, category_statuses=("success",), mode="normal"
    )
    staged = prepare_staged_csv(tmp_path)
    database.publish_collected_batch(
        collected_batch=batch,
        version=1,
        staged_csv=staged,
        published_at=datetime(2026, 7, 17, 14, 5),
    )
    path = tmp_path / "runtime/data/collector.db"
    database.close()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.downgrade(config, "0004_product_image_url")
    with sqlite3.connect(path) as connection:
        tables = (
            "collection_batches",
            "category_runs",
            "raw_responses",
            "product_rank_entries",
            "product_rank_entry_shops",
        )
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        old = connection.execute(
            "SELECT pay_amount_min_value,pay_amount_max_value,pay_combo_count_min_value,pay_combo_count_max_value FROM product_rank_entries"
        ).fetchone()
    upgrade_database(path)
    backups = list((path.parent / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert (
            backup.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0004_product_image_url"
        )
    with sqlite3.connect(path) as connection:
        assert counts == {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        new = connection.execute(
            "SELECT pay_amount_min_value,pay_amount_max_value,pay_combo_count_min_value,pay_combo_count_max_value,pay_amount_unit,pay_combo_count_unit FROM product_rank_entries"
        ).fetchone()
        snapshot = json.loads(
            connection.execute(
                "SELECT config_snapshot FROM collection_batches"
            ).fetchone()[0]
        )
        assert snapshot["requested_selection"] is None
        assert (
            connection.execute("SELECT platform FROM collection_batches").fetchone()[0]
            == "compass"
        )
        assert json.loads(
            connection.execute("SELECT scope_path FROM category_runs").fetchone()[0]
        ) == ["食品饮料", "二级分类1", "三级分类1"]
    assert tuple(Decimal(str(value)) for value in new[:4]) == tuple(
        Decimal(str(value)) / scale for value, scale in zip(old, [100, 100, 10, 10])
    )
    assert new[4:] == ("CNY", "count")
    assert format_metric_range(
        MetricRange(new[0], new[1], "CNY")
    ) == format_metric_range(
        MetricRange(Decimal(old[0]) / 100, Decimal(old[1]) / 100, "CNY")
    )


def test_task_indexes_are_isolated_and_only_primary_updates_root(tmp_path):
    """Publish through the real exporter contract with observable OSS object keys."""

    # Fake OSS retains uploaded JSON to validate both key isolation and ordering.
    class Uploader:
        settings = SimpleNamespace(valid=True)

        def __init__(self):
            """Track keys and uploaded contents in process memory."""
            self.keys = []
            self.contents = {}

        def public_object_url(self, key):
            """Return a deterministic public URL without credentials."""
            return f"https://example.invalid/{key}"

        def upload_public_file(self, *, file_path, object_key, **kwargs):
            """Record atomically complete local files in upload order."""
            self.keys.append(object_key)
            self.contents[object_key] = file_path.read_bytes()

    uploader = Uploader()
    publisher = WebPublisher(
        WebPublicationSettings(enabled=True), uploader, runtime_root=tmp_path
    )
    csv = tmp_path / "result.csv"
    csv.write_text(
        "分类,排名,商品缩略图,商品,店铺名称,用户支付金额,成交件数,首次上榜\n",
        encoding="utf-8-sig",
    )
    arguments = dict(
        csv_path=csv,
        business_date=date(2026, 9, 30),
        published_at=datetime(2026, 9, 30, 14),
        successful_category_count=1,
        failed_category_count=0,
        item_count=0,
        platform="compass",
    )
    publisher.publish(
        **arguments, task_id="primary", batch_id="a" * 32, update_legacy_index=True
    )
    primary = uploader.contents["compass/web/latest.json"]
    publisher.publish(**arguments, task_id="other", batch_id="b" * 32)
    assert uploader.contents["compass/web/latest.json"] == primary
    assert "compass/web/compass/primary/latest.json" in uploader.keys
    assert "compass/web/compass/other/latest.json" in uploader.keys
    assert uploader.keys.count("compass/web/latest.json") == 1
    assert uploader.keys.index(
        "compass/web/compass/primary/latest.json"
    ) < uploader.keys.index("compass/web/latest.json")


def test_retention_handles_old_and_platform_directories(tmp_path):
    """Clean expired raw trees while retaining official data and login profiles."""
    config = load_config(Path("config/tasks.yaml"))
    # Both historical and namespaced layouts follow the same retention cutoff.
    for relative in [
        "raw/2026-01-01/old",
        "raw/compass/2026-01-01/task",
        "raw/compass/2026-09-30/task",
        "exports/compass/2026-01-01/task",
        "browser-profile",
    ]:
        directory = tmp_path / relative
        directory.mkdir(parents=True)
        (directory / "sentinel").write_text("keep")
    summary = cleanup_runtime(tmp_path, config.retention, now=datetime(2026, 9, 30))
    assert summary.raw_directories == 2
    assert (tmp_path / "raw/compass/2026-09-30/task/sentinel").exists()
    assert (tmp_path / "exports/compass/2026-01-01/task/sentinel").exists()
    assert (tmp_path / "browser-profile/sentinel").exists()


def test_shared_collection_accepts_one_level_non_compass_adapter(tmp_path):
    """A different response shape and scope depth work without Compass parameters."""
    from compass_collector.category_batch import prepare_category_batch
    from compass_collector.category_collection import collect_category_batch
    from compass_collector.models import (
        CategoryDiscoveryResult,
        DiscoveredScope,
        ProductRankEntry,
    )
    from compass_collector.platforms.contracts import DiscoveryCapture, PageCapture
    from compass_collector.runtime_logging import RuntimeLogger

    # Only public contracts connect this test adapter to shared orchestration.
    class OtherAdapter:
        def discover_scopes(self, task):
            """Return a single-level native category and an unrelated raw shape."""
            return DiscoveryCapture(
                CategoryDiscoveryResult(
                    None,
                    None,
                    (
                        DiscoveredScope(
                            1, "native-key", ("单层类目",), {"native_id": "key"}
                        ),
                    ),
                ),
                {"native_categories": ["key"]},
            )

        def collect_scope(self, task, scope, business_date):
            """Yield actual-unit metrics with opaque safe cursor metadata."""
            entry = ProductRankEntry(
                1,
                datetime(2026, 9, 30, 14),
                1,
                "native-product",
                "商品",
                False,
                MetricRange(Decimal("12.5"), Decimal("25"), "CNY"),
                MetricRange(Decimal(1), Decimal(2), "count"),
                (),
            )
            yield PageCapture(
                1,
                1,
                1,
                entry.captured_at,
                (entry,),
                {"native_items": ["native-product"]},
                {"cursor": "start"},
            )

    config = load_config(Path("config/tasks.yaml"))
    task = config.tasks[0].model_copy(update={"platform": "test_platform"})
    path = tmp_path / "db.sqlite"
    upgrade_database(path)
    database = Database(path)
    adapter = OtherAdapter()
    try:
        prepared = prepare_category_batch(
            runtime_root=tmp_path,
            batch_id="generic-batch",
            task=task,
            business_date=date(2026, 9, 30),
            planned_at=datetime(2026, 9, 30, 14),
            mode="dry_run",
            client=adapter,
            database=database,
            runtime_logger=RuntimeLogger(tmp_path / "logs"),
        )
        collected = collect_category_batch(
            prepared_batch=prepared,
            task=task,
            client=adapter,
            database=database,
            runtime_logger=RuntimeLogger(tmp_path / "logs"),
        )
        assert len(collected.category_runs[0].entries) == 1
        assert prepared.storage.manifest["categories"][0]["scope_path"] == ["单层类目"]
        assert prepared.storage.manifest["platform"] == "test_platform"
        with sqlite3.connect(path) as connection:
            assert json.loads(
                connection.execute("SELECT safe_params FROM raw_responses").fetchone()[
                    0
                ]
            ) == {"cursor": "start"}
            assert json.loads(
                connection.execute(
                    "SELECT platform_metadata FROM category_runs"
                ).fetchone()[0]
            ) == {"native_id": "key"}
    finally:
        database.close()
