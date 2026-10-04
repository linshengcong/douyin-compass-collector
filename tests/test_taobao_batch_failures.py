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
        # 成功夹在错误中时仍要经过真实共享 raw/PostgreSQL 生命周期。
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


def test_business_failures_finish_all_categories_before_retry():
    """连续业务失败仍完成首轮，并在两轮补采后终止。"""
    prepared, task, adapter, database, logger, storage = execute([business_failure() for _ in range(4)])
    with pytest.raises(CategoryBatchCollectionError) as failure:
        collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                               database=database, runtime_logger=logger)
    assert failure.value.cause.category == "taobao_business_error"
    assert adapter.calls == ["category-1", "category-2", "category-3", "category-4"] * 3
    assert len(storage.failure_calls) == 12 and len(database.failure_calls) == 12
    assert database.terminate_calls[0]["status"] == "failed"
    assert not any(event["event"] == "platform_unavailable_circuit_opened" for event in logger.events)


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
    assert adapter.calls[:6] == [f"category-{i}" for i in range(1, 7)]
    assert len(adapter.calls) == (14 if separator is None else 16)
    assert result.failed_category_count == (4 if separator is None else 5)
    assert len(result.category_runs) == (2 if separator is None else 1)
    assert not database.terminate_calls
    assert not any(event["event"] == "platform_unavailable_circuit_opened" for event in logger.events)


def test_failed_categories_retry_only_after_full_round(monkeypatch):
    """首轮完整遍历后补采，成功分类不重复，最终失败数按分类计算。"""
    # 第一分类第二轮成功，第三分类第三轮成功。
    prepared, task, adapter, database, logger, storage = execute([None, None, None])
    # 逐次记录分类尝试，验证真实编排顺序。
    counts = {}
    original = adapter.collect_scope

    def intermittent(task, scope, business_date):
        """按分类注入有限失败，成功仍走真实淘宝解析。"""
        counts[scope.key] = counts.get(scope.key, 0) + 1
        if counts[scope.key] < {"category-1": 2, "category-3": 3}.get(scope.key, 1):
            adapter.calls.append(scope.key)
            raise HttpRequestError("Synthetic timeout", category="page_response_timeout")
        yield from original(task, scope, business_date)

    monkeypatch.setattr(adapter, "collect_scope", intermittent)
    result = collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                                    database=database, runtime_logger=logger)
    assert adapter.calls == ["category-1", "category-2", "category-3", "category-1", "category-3", "category-3"]
    assert result.failed_category_count == 0
    assert [run.plan.category.key for run in result.category_runs] == ["category-1", "category-2", "category-3"]


def test_persistent_failures_exhaust_two_full_retry_rounds():
    """持续业务失败也先遍历全部分类，最多补采两轮。"""
    # 淘宝业务错误不能提前熔断，全部失败时仍禁止发布。
    prepared, task, adapter, database, logger, storage = execute([business_failure() for _ in range(4)])
    with pytest.raises(CategoryBatchCollectionError):
        collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                               database=database, runtime_logger=logger)
    assert adapter.calls == [f"category-{i}" for i in range(1, 5)] * 3
    assert len(database.failure_calls) == 12


@pytest.mark.parametrize("interrupt", ["auth", "stop"])
def test_task_interrupt_during_retry_prevents_remaining_retry_categories(monkeypatch, interrupt):
    """补采中的登录失效和用户停止必须终止任务，不能当成分类失败继续。"""
    from compass_collector.errors import AuthRequiredError, CollectionInterruptedError
    from compass_collector.run_control import CollectionControl
    # 首轮两分类都失败，第一轮补采第一分类时中断。
    prepared, task, adapter, database, logger, storage = execute([None, None])
    control = CollectionControl()
    counts = {}

    def fail_or_interrupt(task, scope, business_date):
        """记录调用顺序并在第一个补采分类触发任务级中断。"""
        adapter.calls.append(scope.key)
        counts[scope.key] = counts.get(scope.key, 0) + 1
        if counts[scope.key] == 1:
            raise HttpRequestError("Synthetic timeout", category="page_response_timeout")
        if interrupt == "auth":
            raise AuthRequiredError("Synthetic login expired", category="auth_required")
        control.request_stop()
        raise CollectionInterruptedError("Synthetic stop", category="interrupted")
        yield  # 保持与生产适配器一致的惰性迭代入口。

    monkeypatch.setattr(adapter, "collect_scope", fail_or_interrupt)
    with pytest.raises(CategoryBatchCollectionError) as caught:
        collect_category_batch(prepared_batch=prepared, task=task, client=adapter,
                               database=database, runtime_logger=logger, control=control)
    assert adapter.calls == ["category-1", "category-2", "category-1"]
    assert caught.value.cause.category == ("auth_required" if interrupt == "auth" else "interrupted")
    assert database.terminate_calls[-1]["status"] == caught.value.cause.category
