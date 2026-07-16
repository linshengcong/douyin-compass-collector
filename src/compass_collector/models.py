"""Immutable domain records shared by parsing, persistence, and CSV export."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from compass_collector.raw_storage import RunStorage


@dataclass(frozen=True, slots=True)
class MetricRange:
    """Preserve one platform value range without persistence-layer conversion."""

    min_value: int
    max_value: int
    unit: str


@dataclass(frozen=True, slots=True)
class ProductShop:
    """Preserve a product-shop relationship and its source order."""

    position: int
    shop_id: str
    shop_name: str


@dataclass(frozen=True, slots=True)
class ProductRankEntry:
    """Represent one validated product ranking row."""

    page_no: int
    captured_at: datetime
    rank: int
    product_id: str
    product_name: str
    newly_on_ranking: bool
    pay_amount: MetricRange
    pay_combo_count: MetricRange
    shops: tuple[ProductShop, ...]


@dataclass(frozen=True, slots=True)
class RawPageRecord:
    """Describe one validated raw response file for database indexing."""

    page_no: int
    path: Path
    item_count: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class CollectedTaskRun:
    """Bundle a fully validated task attempt before official publication."""

    task_id: str
    business_date: date
    started_at: datetime
    finished_at: datetime
    storage: RunStorage
    entries: tuple[ProductRankEntry, ...]
    raw_pages: tuple[RawPageRecord, ...]
