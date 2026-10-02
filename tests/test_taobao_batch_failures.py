"""Shared task lifecycle must stop repeated Taobao business failures safely."""

import pytest

from compass_collector.category_collection import collect_category_batch
from compass_collector.config import TaskConfig
from compass_collector.errors import CategoryBatchCollectionError, HttpRequestError, ResponseContractError
from compass_collector.platforms.contracts import PageCapture
from compass_collector.platforms.taobao_product_rank import parse_page_entries
from test_category_collection import (
    FakeBatchStorage, FakeDatabase, FakeRuntimeLogger, build_prepared_batch, PLANNED_AT,
)
from test_taobao_product_rank import page_payload


class OutcomesAdapter:
    """Return normalized success or a safe failure, without Compass request parsing."""

    def __init__(self, outcomes):
        """Track precisely how far shared orchestration advances."""
        # None 表示完整单页成功，错误对象表示本地留档后分类失败。
        self.outcomes = outcomes
        self.calls = []

    def collect_scope(self, task, scope, business_date):
        """Use real Taobao parsing for successful normalized pages."""
        self.calls.append(scope.key)
        # 以分类发现次序索引，避免复用抖音 business query 编码。
        outcome = self.outcomes[scope.discovery_order - 1]
        if outcome is not None:
            raise outcome
        # 成功夹在错误中时仍要经过真实共享 raw/SQLite 生命周期。
        payload = page_payload(total=1)
        entries = tuple(parse_page_entries(payload, page_no=1, captured_at=PLANNED_AT))
        yield PageCapture(1, 1, 1, PLANNED_AT, entries, payload, {"pageSize": 20, "page": 1})


def business_failure():
    """Unknown codes retain their local body, without guessing login semantics."""
    return ResponseContractError("Unknown Taobao business response", category="taobao_business_error",
                                 response_body=b'{"code":12345,"message":"synthetic"}')


def execute(outcomes):
    """Run the production batch loop against observable lifecycle boundaries."""
    # 所有状态只存在测试替身，禁止触发真实账号请求或正式发布。
    events = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    prepared = build_prepared_batch(category_count=len(outcomes), storage=storage)
    task = TaskConfig(id=prepared.task_id, platform="taobao", display_name="合成淘宝任务", schedule="0 14 * * *")
    adapter = OutcomesAdapter(outcomes)
    logger = FakeRuntimeLogger()
    return prepared, task, adapter, database, logger, storage


def test_three_business_failures_stop_before_fourth_category():
    """The third local failure closes the batch and prevents further page actions."""
    prepared, task, adapter, database, logger, storage = execute([business_failure() for _ in range(4)])
    with pytest.raises(CategoryBatchCollectionError) as failure:
        collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                               database=database, runtime_logger=logger)
    assert failure.value.cause.category == "taobao_business_error"
    assert adapter.calls == ["category-1", "category-2", "category-3"]
    assert len(storage.failure_calls) == 3 and len(database.failure_calls) == 3
    assert database.terminate_calls[0]["status"] == "failed"
    assert any(event["event"] == "platform_unavailable_circuit_opened"
               and "11001" not in event["message"] for event in logger.events)


def test_final_integrity_failure_is_not_reported_as_nonexistent_next_page(monkeypatch):
    """A generator's final validation failure has no requested page beyond its declared total."""
    # 保留真实共享持久化过程，在末页已保存后注入整体完整性失败。
    prepared, task, adapter, database, logger, storage = execute([None])
    original = adapter.collect_scope

    def failing_after_last_page(*args):
        """Yield the complete page then reject the category-wide snapshot."""
        yield from original(*args)
        raise ResponseContractError("Synthetic final duplicate", category="duplicate_rank")

    monkeypatch.setattr(adapter, "collect_scope", failing_after_last_page)
    with pytest.raises(CategoryBatchCollectionError):
        collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                               database=database, runtime_logger=logger)
    assert storage.failure_calls[0]["failed_page"] is None
    assert database.failure_calls[0]["failed_page"] is None


@pytest.mark.parametrize("separator", [None, HttpRequestError("Synthetic timeout", category="page_response_timeout")])
def test_success_or_transport_failure_resets_business_error_sequence(separator):
    """Business-error circuit counts consecutive categories of its own kind only."""
    prepared, task, adapter, database, logger, storage = execute([
        business_failure(), business_failure(), separator,
        business_failure(), business_failure(), None,
    ])
    result = collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                    database=database, runtime_logger=logger)
    assert len(adapter.calls) == 6
    assert len(result.category_runs) == (2 if separator is None else 1)
    assert not database.terminate_calls
    assert not any(event["event"] == "platform_unavailable_circuit_opened" for event in logger.events)
