"""Adapt sanitized response fixtures to the production platform boundary."""

import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from compass_collector.platforms.contracts import DiscoveryCapture, PageCapture
from compass_collector.platforms.compass_categories import parse_category_tree
from compass_collector.platforms.compass_product_rank import (
    build_request_params,
    validate_page_payload,
    parse_page_entries,
    validate_complete_ranking,
)


@dataclass
class FixtureResponse:
    """Hold old fixture bytes without importing the removed HTTP client."""

    payload: dict
    body: bytes
    status_code: int


class FixtureAdapter:
    """Convert deterministic fixture providers to normalized adapter pages."""

    def discover_scopes(self, task):
        """Read one fixture tree; parser failures stay at the platform boundary."""
        # Existing lifecycle fixtures intentionally cover the full fixture tree.
        response = self.get_category_tree({})
        try:
            discovery = parse_category_tree(response.payload)
        except Exception as error:
            error.discovery_payload = response.payload
            raise
        return DiscoveryCapture(discovery, response.payload)

    def collect_scope(self, task, scope, business_date):
        """Yield pages only after platform parsing, including final rank validation."""
        # All fixture state remains local to the iterator.
        total = None
        entries = []
        page_no = 1
        target_pages = 1
        while page_no <= target_pages:
            params = build_request_params(task, scope, business_date, page_no)
            response = self.get_product_rank_page(task, params)
            contract = validate_page_payload(
                response.payload, requested_page=page_no, expected_total=total
            )
            total, target_pages = contract.api_total, contract.target_page_count
            captured_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            page_entries = tuple(
                parse_page_entries(
                    response.payload, page_no=page_no, captured_at=captured_at
                )
            )
            entries.extend(page_entries)
            yield PageCapture(
                page_no,
                total,
                target_pages,
                captured_at,
                page_entries,
                response.payload,
                params,
            )
            page_no += 1
        validate_complete_ranking(entries, api_total=total)
