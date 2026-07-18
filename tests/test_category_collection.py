"""Stage-three serial category ranking orchestration tests."""

import json
import time
from datetime import date, datetime
from pathlib import Path
from threading import Lock, get_ident
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from compass_collector.category_batch import PreparedCategoryBatch
from compass_collector.category_collection import collect_category_batch
from compass_collector.config import load_config
from compass_collector.errors import (
    AuthRequiredError,
    CategoryBatchCollectionError,
    HttpRequestError,
)
from compass_collector.http_client import HttpJsonResponse
from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
    DiscoveredCategory,
    RawPageRecord,
)
from compass_collector.persistence import (
    CategoryRun,
    CollectionBatch,
    Database,
    ProductRankEntryModel,
    RawResponse,
    upgrade_database,
)
from compass_collector.raw_storage import BatchStorage
from compass_collector.run_control import CollectionControl


# 测试批次日期和时间统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 固定业务日期避免请求参数断言受运行日期影响。
BUSINESS_DATE = date(2026, 7, 17)
# 固定计划时间用于构造阶段二已经准备好的批次。
PLANNED_AT = datetime(2026, 7, 17, 14, 0, tzinfo=SHANGHAI_TIMEZONE)


def build_category(order: int, *, level1_category_id: str = "13") -> DiscoveredCategory:
    """Create one deterministic dynamic level-three category."""

    # 分类 ID 随发现顺序变化，方便精确断言分类顺序。
    category_id = f"category-{order}"
    # 默认一级名称维持真实 fixture 的食品饮料，其他 ID 仅用于并发分组测试。
    level1_category_name = (
        "食品饮料" if level1_category_id == "13" else f"一级分类{level1_category_id}"
    )
    return DiscoveredCategory(
        discovery_order=order,
        level1_category_id=level1_category_id,
        level1_category_name=level1_category_name,
        level2_category_id=f"level2-{order}",
        level2_category_name=f"二级分类{order}",
        category_id=category_id,
        category_name=f"三级分类{order}",
    )


def build_page_payload(
    *,
    category_id: str,
    page_no: int,
    total: int,
) -> dict[str, Any]:
    """Build one complete sanitized ranking page for the requested total."""

    # 当前页起始排名按固定十条分页计算。
    start_rank = ((page_no - 1) * 10) + 1
    # total=0 时第一页返回空数组，末页按真实剩余条数裁剪。
    item_count = max(0, min(10, total - ((page_no - 1) * 10)))
    # 商品行满足真实解析器验证的完整字段契约。
    data_result: list[dict[str, Any]] = []
    for offset in range(item_count):
        # rank 同时作为商品 ID 的稳定去重后缀。
        rank = start_rank + offset
        data_result.append(
            {
                "product_info": {
                    "id": f"{category_id}-product-{rank}",
                    "name": f"商品{rank}",
                    "rank": rank,
                    "newly_on_ranking": False,
                    "shop_list": [
                        {
                            "shop_id": f"shop-{rank}",
                            "shop_name": f"店铺{rank}",
                        }
                    ],
                },
                "new_pay_amt": {
                    "value_range": [
                        {"value": rank * 100, "unit": "price"},
                        {"value": rank * 100 + 50, "unit": "price"},
                    ]
                },
                "pay_combo_cnt": {
                    "value_range": [
                        {"value": rank, "unit": "number"},
                        {"value": rank + 1, "unit": "number"},
                    ]
                },
            }
        )
    return {
        "st": 0,
        "data": {
            "data_result": data_result,
            "page_result": {
                "page_no": page_no,
                "page_size": 10,
                "total": total,
            },
        },
    }


class FakeBatchStorage:
    """Record storage calls without writing repository or runtime files."""

    def __init__(
        self,
        events: list[tuple[Any, ...]],
        *,
        fail_once_operations: set[str] | None = None,
    ) -> None:
        """Share one event stream with the fake database for order assertions."""

        # events 精确验证 raw -> SQLite -> Manifest 调用顺序。
        self.events = events
        # failure_calls 用于确认每个失败分类只留档一次。
        self.failure_calls: list[dict[str, Any]] = []
        # write_thread_ids 验证所有 raw 与 Manifest 写入仍由采集 owner 执行。
        self.write_thread_ids: list[int] = []
        # fail_once_operations 模拟一次瞬时 Manifest 原子替换失败。
        self.fail_once_operations = set(fail_once_operations or ())

    def write_category_page(
        self,
        category_run_id: str,
        page_no: int,
        payload: dict[str, Any],
    ) -> Path:
        """Return a safe synthetic path after recording the raw boundary."""

        self.write_thread_ids.append(get_ident())
        self.events.append(("raw", category_run_id, page_no))
        return Path(f"/runtime/{category_run_id}/page-{page_no:03d}.json.gz")

    def sync_collection_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Record the Manifest synchronization after each SQLite snapshot."""

        # 指定操作第一次失败后移除标记，使同快照重试可以成功。
        self.write_thread_ids.append(get_ident())
        if snapshot["operation"] in self.fail_once_operations:
            self.fail_once_operations.remove(snapshot["operation"])
            raise OSError("simulated transient manifest failure")
        self.events.append(
            (
                "manifest",
                snapshot["operation"],
                snapshot.get("category_run_id"),
                snapshot.get("page_no"),
            )
        )

    def save_category_failure(self, **failure_fields: Any) -> None:
        """Keep only the sanitized failure arguments for assertions."""

        self.failure_calls.append(dict(failure_fields))


class FakeDatabase:
    """Expose the fixed stage-three persistence API with observable calls."""

    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        """Initialize lifecycle call collections and the shared order stream."""

        # events 与 FakeBatchStorage 共用，形成跨层全序列。
        self.events = events
        # start_calls 证明分类没有并行或重复启动。
        self.start_calls: list[str] = []
        # success_calls 只包含完整榜单分类。
        self.success_calls: list[str] = []
        # failure_calls 只包含第一个和第二个普通失败。
        self.failure_calls: list[dict[str, Any]] = []
        # terminate_calls 用于核对 auth/interrupted/内部错误的原子收口。
        self.terminate_calls: list[dict[str, Any]] = []
        # write_thread_ids 验证 SQLite 生命周期没有落到网络 worker。
        self.write_thread_ids: list[int] = []

    @staticmethod
    def _snapshot(
        operation: str,
        *,
        category_run_id: str | None = None,
        page_no: int | None = None,
    ) -> dict[str, Any]:
        """Return the minimal snapshot consumed by the fake Manifest."""

        return {
            "operation": operation,
            "category_run_id": category_run_id,
            "page_no": page_no,
        }

    def start_category_run(
        self,
        *,
        category_run_id: str,
        started_at: datetime,
    ) -> dict[str, Any]:
        """Record one pending-to-running transition."""

        self.write_thread_ids.append(get_ident())
        self.start_calls.append(category_run_id)
        self.events.append(("sqlite_start", category_run_id))
        return self._snapshot("start", category_run_id=category_run_id)

    def record_category_page(
        self,
        *,
        category_run_id: str,
        raw_page: RawPageRecord,
        api_total: int,
        target_page_count: int,
    ) -> dict[str, Any]:
        """Record one raw page index after its synthetic file path exists."""

        self.write_thread_ids.append(get_ident())
        self.events.append(("sqlite_page", category_run_id, raw_page.page_no))
        return self._snapshot(
            "page",
            category_run_id=category_run_id,
            page_no=raw_page.page_no,
        )

    def finish_category_success(
        self,
        *,
        category_run_id: str,
        api_total: int,
        target_page_count: int,
        finished_at: datetime,
    ) -> dict[str, Any]:
        """Record one fully validated category success."""

        self.write_thread_ids.append(get_ident())
        self.success_calls.append(category_run_id)
        self.events.append(("sqlite_success", category_run_id))
        return self._snapshot("success", category_run_id=category_run_id)

    def finish_category_failure(
        self,
        *,
        category_run_id: str,
        failed_page: int,
        error_category: str,
        finished_at: datetime,
    ) -> dict[str, Any]:
        """Record one skippable ordinary category failure."""

        # 失败详情不含异常正文或响应内容。
        failure_call = {
            "category_run_id": category_run_id,
            "failed_page": failed_page,
            "error_category": error_category,
        }
        self.write_thread_ids.append(get_ident())
        self.failure_calls.append(failure_call)
        self.events.append(("sqlite_failure", category_run_id, failed_page))
        return self._snapshot("failure", category_run_id=category_run_id)

    def terminate_collection_batch(self, **terminal_fields: Any) -> dict[str, Any]:
        """Record one atomic terminal transaction and return its snapshot."""

        self.write_thread_ids.append(get_ident())
        self.terminate_calls.append(dict(terminal_fields))
        self.events.append(
            (
                "sqlite_terminate",
                terminal_fields["status"],
                terminal_fields.get("current_category_run_id"),
            )
        )
        return self._snapshot(
            "terminate",
            category_run_id=terminal_fields.get("current_category_run_id"),
            page_no=terminal_fields.get("failed_page"),
        )


class FakeRuntimeLogger:
    """Capture safe events without creating daily JSONL files."""

    def __init__(self) -> None:
        """Initialize the collected event list."""

        # events 只保存已由编排层构造的脱敏字段。
        self.events: list[dict[str, Any]] = []

    def emit(self, **event_fields: Any) -> None:
        """Store one structured event exactly as received."""

        self.events.append(dict(event_fields))


class FakeRankingClient:
    """Serve category-specific totals or safe request failures without retries."""

    def __init__(
        self,
        behavior_by_category: dict[str, int | Exception],
        *,
        page_fetch_workers: int = 1,
        level1_fetch_workers: int = 1,
        page_fetch_wait_timeout_seconds: float = 45,
        delay_by_page: dict[int, float] | None = None,
        failure_by_page: dict[int, Exception] | None = None,
        stop_after_response_for: str | None = None,
        control: CollectionControl | None = None,
    ) -> None:
        """Store deterministic category behavior and optional stop injection."""

        # behavior_by_category 使用动态 category_id 选择结果。
        self.behavior_by_category = behavior_by_category
        # page_fetch_workers 模拟真实客户端暴露给编排层的分页 worker 数。
        self.page_fetch_workers = page_fetch_workers
        # level1_fetch_workers 模拟一级分类组的最大并发调度数。
        self.level1_fetch_workers = level1_fetch_workers
        # 分页绝对等待上限用于模拟底层 HTTP 无进度的看门狗边界。
        self.page_fetch_wait_timeout_seconds = page_fetch_wait_timeout_seconds
        # delay_by_page 用于制造乱序响应，验证主线程顺序持久化。
        self.delay_by_page = dict(delay_by_page or {})
        # failure_by_page 用于模拟单个并发分页失败。
        self.failure_by_page = dict(failure_by_page or {})
        # calls 保留请求启动记录，并发分页不依赖它断言落盘顺序。
        self.calls: list[tuple[str, int]] = []
        # _lock 保护并发测试中的调用、active 和 max_active 统计。
        self._lock = Lock()
        # active_requests 是当前仍未返回的模拟 HTTP 请求数。
        self.active_requests = 0
        # max_active_requests 用于断言分页预取确实发生且未超过上限。
        self.max_active_requests = 0
        # stop_after_response_for 模拟响应期间用户点击停止。
        self.stop_after_response_for = stop_after_response_for
        # control 与编排层共享同一个中止信号。
        self.control = control

    def get_product_rank_page(
        self,
        task: Any,
        params: dict[str, str | int],
    ) -> HttpJsonResponse:
        """Return one generated page or raise the configured error once requested."""

        # 请求参数必须携带二级、三级分类组成的完整级联路径。
        category_path = str(params["category_id"])
        # category_path_parts 强制集成链路保持真实的两段格式。
        category_path_parts = category_path.split(",")
        if len(category_path_parts) != 2 or not all(category_path_parts):
            raise AssertionError("category_id must contain level-two and level-three IDs")
        # 行为映射继续使用级联路径末段的三级叶子 ID。
        category_id = category_path_parts[-1]
        # page_no 的配置契约保证为整数。
        page_no = int(params["page_no"])
        with self._lock:
            self.calls.append((category_id, page_no))
            self.active_requests += 1
            self.max_active_requests = max(
                self.max_active_requests,
                self.active_requests,
            )
        try:
            # delay_by_page 在请求已登记后等待，制造稳定的并发重叠。
            delay_seconds = self.delay_by_page.get(page_no, 0)
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            # 单页失败优先于分类级行为，用于测试并发分页失败定位。
            if page_no in self.failure_by_page:
                raise self.failure_by_page[page_no]
            # 当前分类行为在测试开始前完整配置。
            behavior = self.behavior_by_category[category_id]
            if isinstance(behavior, Exception):
                raise behavior
            # total 驱动真实分页契约生成每一页条数。
            payload = build_page_payload(
                category_id=category_id,
                page_no=page_no,
                total=behavior,
            )
            if category_id == self.stop_after_response_for and self.control is not None:
                self.control.request_stop()
            # body 仅用于失败路径，本测试使用同一脱敏 JSON 占位内容。
            return HttpJsonResponse(payload=payload, body=b"sanitized", status_code=200)
        finally:
            with self._lock:
                self.active_requests -= 1


def build_prepared_batch(
    *,
    category_count: int,
    storage: FakeBatchStorage,
    level1_category_ids: tuple[str, ...] | None = None,
) -> PreparedCategoryBatch:
    """Create one stage-two result without invoking category discovery."""

    # 分类按接口发现顺序构造。
    # 未指定时沿用单一级分类 fixture，指定时用于构造跨一级分类调度场景。
    category_level1_ids = level1_category_ids or ("13",) * category_count
    categories = tuple(
        build_category(order, level1_category_id=category_level1_ids[order - 1])
        for order in range(1, category_count + 1)
    )
    # 每个分类运行 ID 在批次准备阶段已经固定。
    plans = tuple(
        CategoryRunPlan(category_run_id=f"run-{category.discovery_order}", category=category)
        for category in categories
    )
    # discovery 只提供阶段三不再请求的根与分类快照。
    discovery = CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=categories,
    )
    return PreparedCategoryBatch(
        batch_id="batch-stage-three",
        task_id="product_hot_sale_all_level3",
        business_date=BUSINESS_DATE,
        planned_at=PLANNED_AT,
        mode="normal",
        started_at=PLANNED_AT,
        storage=storage,  # type: ignore[arg-type]
        discovery=discovery,
        category_run_plans=plans,
    )


def load_task() -> Any:
    """Load the real dynamic-category task used by request parameter assembly."""

    return load_config(Path("config/tasks.yaml")).tasks[0]


def test_collects_more_than_two_hundred_items_in_strict_page_order() -> None:
    """Collect all twenty-one pages for a 201-item category without a cap."""

    # 跨层事件流用于验证每页持久化先后顺序。
    events: list[tuple[Any, ...]] = []
    # FakeStorage 和 FakeDatabase 共用同一事件列表。
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    # 单分类 total=201 必须请求完整二十一页。
    client = FakeRankingClient({"category-1": 201})
    # 阶段二输入和运行日志均为内存对象。
    prepared_batch = build_prepared_batch(category_count=1, storage=storage)
    logger = FakeRuntimeLogger()

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=logger,  # type: ignore[arg-type]
    )

    assert client.calls == [("category-1", page_no) for page_no in range(1, 22)]
    assert len(result.category_runs) == 1
    assert result.category_runs[0].api_total == 201
    assert result.category_runs[0].target_page_count == 21
    assert len(result.category_runs[0].entries) == 201
    assert len(result.category_runs[0].raw_pages) == 21
    assert result.failed_category_count == 0
    assert database.success_calls == ["run-1"]
    assert database.terminate_calls == []
    # 每页三层操作必须连续保持 raw -> SQLite -> Manifest。
    page_events = [event for event in events if event[0] in {"raw", "sqlite_page"} or (
        event[0] == "manifest" and event[1] == "page"
    )]
    assert page_events[:3] == [
        ("raw", "run-1", 1),
        ("sqlite_page", "run-1", 1),
        ("manifest", "page", "run-1", 1),
    ]


def test_prefetched_pages_persist_in_page_order_after_out_of_order_responses() -> None:
    """Fetch later pages concurrently while committing raw and SQLite in order."""

    # 延迟让第 5 页先于第 2 页返回，验证主线程按页码缓冲落盘。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {"category-1": 51},
        page_fetch_workers=4,
        delay_by_page={2: 0.08, 3: 0.06, 4: 0.04, 5: 0.02, 6: 0.01},
    )
    prepared_batch = build_prepared_batch(category_count=1, storage=storage)

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    page_events = [
        event
        for event in events
        if event[0] in {"raw", "sqlite_page"}
        or (event[0] == "manifest" and event[1] == "page")
    ]
    assert result.category_runs[0].target_page_count == 6
    assert client.max_active_requests > 1
    assert client.max_active_requests <= 4
    assert page_events == [
        ("raw", "run-1", 1),
        ("sqlite_page", "run-1", 1),
        ("manifest", "page", "run-1", 1),
        ("raw", "run-1", 2),
        ("sqlite_page", "run-1", 2),
        ("manifest", "page", "run-1", 2),
        ("raw", "run-1", 3),
        ("sqlite_page", "run-1", 3),
        ("manifest", "page", "run-1", 3),
        ("raw", "run-1", 4),
        ("sqlite_page", "run-1", 4),
        ("manifest", "page", "run-1", 4),
        ("raw", "run-1", 5),
        ("sqlite_page", "run-1", 5),
        ("manifest", "page", "run-1", 5),
        ("raw", "run-1", 6),
        ("sqlite_page", "run-1", 6),
        ("manifest", "page", "run-1", 6),
    ]


def test_level_one_groups_fetch_concurrently_but_persist_on_owner_thread() -> None:
    """Run two top-level groups together without moving storage or SQLite writes off-thread."""

    # 两个一级分类各含一个空榜单，第一页延迟确保网络请求产生稳定重叠。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {"category-1": 0, "category-2": 0},
        level1_fetch_workers=2,
        delay_by_page={1: 0.05},
    )
    prepared_batch = build_prepared_batch(
        category_count=2,
        storage=storage,
        level1_category_ids=("13", "25"),
    )
    owner_thread_id = get_ident()

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    assert client.max_active_requests == 2
    assert [run.plan.category_run_id for run in result.category_runs] == [
        "run-1",
        "run-2",
    ]
    assert set(database.write_thread_ids) == {owner_thread_id}
    assert set(storage.write_thread_ids) == {owner_thread_id}


def test_parallel_group_auth_failure_does_not_start_a_waiting_third_group() -> None:
    """Stop group scheduling after auth failure while preserving its terminal reason."""

    # 两个槽位先启动前两个一级分类；第三组必须等待可用槽位而不能抢跑。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {
            "category-1": AuthRequiredError(
                "Compass authentication is required",
                category="auth_required",
                status_code=401,
                response_body=b"sanitized-auth-response",
            ),
            "category-2": 0,
            "category-3": 0,
        },
        level1_fetch_workers=2,
        delay_by_page={1: 0.03},
    )
    prepared_batch = build_prepared_batch(
        category_count=3,
        storage=storage,
        level1_category_ids=("13", "25", "37"),
    )

    with pytest.raises(CategoryBatchCollectionError) as error_info:
        collect_category_batch(
            prepared_batch=prepared_batch,
            task=load_task(),
            client=client,  # type: ignore[arg-type]
            database=database,  # type: ignore[arg-type]
            runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
        )

    assert error_info.value.cause.category == "auth_required"
    assert all(category_id != "category-3" for category_id, _page_no in client.calls)
    assert database.terminate_calls[0]["status"] == "auth_required"


def test_concurrent_page_failure_records_the_failed_page() -> None:
    """Report the failing concurrent page without publishing later buffered pages."""

    # 第 3 页在并发预取中失败，分类按普通失败收口并继续批次语义。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {"category-1": 41, "category-2": 0},
        page_fetch_workers=4,
        delay_by_page={2: 0.05, 3: 0.01, 4: 0.05, 5: 0.05},
        failure_by_page={
            3: HttpRequestError(
                "HTTP request timed out",
                category="timeout",
            )
        },
    )
    prepared_batch = build_prepared_batch(category_count=2, storage=storage)

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    persisted_pages = [
        (event[1], event[2]) for event in events if event[0] == "sqlite_page"
    ]
    assert [run.plan.category_run_id for run in result.category_runs] == ["run-2"]
    assert result.failed_category_count == 1
    assert database.failure_calls == [
        {
            "category_run_id": "run-1",
            "failed_page": 3,
            "error_category": "timeout",
        }
    ]
    assert persisted_pages == [("run-1", 1), ("run-2", 1)]


def test_stalled_prefetch_times_out_and_allows_later_category_to_continue() -> None:
    """Fail one no-progress prefetch without blocking later category collection."""

    # 第一个分类的预取页故意慢于绝对上限；第二个分类仍必须被完整采集。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {"category-1": 21, "category-2": 0},
        page_fetch_workers=2,
        page_fetch_wait_timeout_seconds=0.01,
        delay_by_page={2: 0.08, 3: 0.08},
    )
    prepared_batch = build_prepared_batch(category_count=2, storage=storage)

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    assert [run.plan.category_run_id for run in result.category_runs] == ["run-2"]
    assert result.failed_category_count == 1
    assert database.failure_calls == [
        {
            "category_run_id": "run-1",
            "failed_page": 2,
            "error_category": "timeout",
        }
    ]


def test_total_zero_still_saves_one_empty_page() -> None:
    """Treat an empty ranking as a valid one-page complete category."""

    # 空榜单仍完整走 raw、SQLite 和 Manifest。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient({"category-1": 0})
    prepared_batch = build_prepared_batch(category_count=1, storage=storage)

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    assert client.calls == [("category-1", 1)]
    assert result.category_runs[0].api_total == 0
    assert result.category_runs[0].target_page_count == 1
    assert result.category_runs[0].entries == ()
    assert result.category_runs[0].raw_pages[0].item_count == 0


def test_manifest_sync_retries_without_repeating_sqlite_page_write() -> None:
    """Retry the same page snapshot once without replaying its database transaction."""

    # page 快照第一次投影失败，第二次应在同一编排调用内成功。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events, fail_once_operations={"page"})
    database = FakeDatabase(events)
    client = FakeRankingClient({"category-1": 0})
    prepared_batch = build_prepared_batch(category_count=1, storage=storage)

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    assert len(result.category_runs) == 1
    assert events.count(("sqlite_page", "run-1", 1)) == 1
    assert events.count(("manifest", "page", "run-1", 1)) == 1


def test_one_ordinary_failure_continues_without_retry() -> None:
    """Skip one network-failed category and collect all later categories once."""

    # 第一个分类固定网络失败，后两个分类为空榜单成功。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {
            "category-1": HttpRequestError(
                "HTTP request timed out",
                category="timeout",
            ),
            "category-2": 0,
            "category-3": 0,
        }
    )
    prepared_batch = build_prepared_batch(category_count=3, storage=storage)

    result = collect_category_batch(
        prepared_batch=prepared_batch,
        task=load_task(),
        client=client,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
    )

    assert client.calls == [
        ("category-1", 1),
        ("category-2", 1),
        ("category-3", 1),
    ]
    assert [run.plan.category_run_id for run in result.category_runs] == [
        "run-2",
        "run-3",
    ]
    assert result.failed_category_count == 1
    assert database.failure_calls == [
        {
            "category_run_id": "run-1",
            "failed_page": 1,
            "error_category": "timeout",
        }
    ]
    assert len(storage.failure_calls) == 1
    assert database.terminate_calls == []


def test_all_ordinary_failures_terminate_only_after_every_category_is_attempted() -> None:
    """Keep skipping ordinary failures, then fail the batch only when none succeeded."""

    # 四个分类都配置失败；每个分类都应保留失败材料后才收口空结果批次。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {
            category_id: HttpRequestError(
                "HTTP request failed",
                category="network_error",
            )
            for category_id in (
                "category-1",
                "category-2",
                "category-3",
                "category-4",
            )
        }
    )
    prepared_batch = build_prepared_batch(category_count=4, storage=storage)

    with pytest.raises(CategoryBatchCollectionError) as error_info:
        collect_category_batch(
            prepared_batch=prepared_batch,
            task=load_task(),
            client=client,  # type: ignore[arg-type]
            database=database,  # type: ignore[arg-type]
            runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
        )

    assert error_info.value.cause.category == "network_error"
    assert client.calls == [
        ("category-1", 1),
        ("category-2", 1),
        ("category-3", 1),
        ("category-4", 1),
    ]
    assert [call["category_run_id"] for call in database.failure_calls] == [
        "run-1",
        "run-2",
        "run-3",
        "run-4",
    ]
    assert database.terminate_calls == [
        {
            "batch_id": "batch-stage-three",
            "status": "failed",
            "error_category": "network_error",
            "finished_at": database.terminate_calls[0]["finished_at"],
            "current_category_run_id": None,
            "failed_page": None,
        }
    ]
    assert len(storage.failure_calls) == 4


def test_auth_failure_stops_before_the_next_category() -> None:
    """Terminate auth_required immediately and never request later categories."""

    # 第一分类鉴权失效，第二分类不得启动。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    client = FakeRankingClient(
        {
            "category-1": AuthRequiredError(
                "Compass authentication is required",
                category="auth_required",
                status_code=401,
                response_body=b"sanitized-auth-response",
            ),
            "category-2": 0,
        }
    )
    prepared_batch = build_prepared_batch(category_count=2, storage=storage)

    with pytest.raises(CategoryBatchCollectionError) as error_info:
        collect_category_batch(
            prepared_batch=prepared_batch,
            task=load_task(),
            client=client,  # type: ignore[arg-type]
            database=database,  # type: ignore[arg-type]
            runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
        )

    assert error_info.value.cause.category == "auth_required"
    assert client.calls == [("category-1", 1)]
    assert database.start_calls == ["run-1"]
    assert database.failure_calls == []
    assert database.terminate_calls[0]["status"] == "auth_required"
    assert database.terminate_calls[0]["current_category_run_id"] == "run-1"
    assert storage.failure_calls[0]["response_body"] == b"sanitized-auth-response"


def test_stop_after_response_marks_batch_interrupted_without_retry() -> None:
    """Persist an accepted page, then honor a stop before category success."""

    # control 在客户端返回第一页时切换为停止状态。
    events: list[tuple[Any, ...]] = []
    storage = FakeBatchStorage(events)
    database = FakeDatabase(events)
    control = CollectionControl()
    client = FakeRankingClient(
        {"category-1": 0, "category-2": 0},
        stop_after_response_for="category-1",
        control=control,
    )
    prepared_batch = build_prepared_batch(category_count=2, storage=storage)

    with pytest.raises(CategoryBatchCollectionError) as error_info:
        collect_category_batch(
            prepared_batch=prepared_batch,
            task=load_task(),
            client=client,  # type: ignore[arg-type]
            database=database,  # type: ignore[arg-type]
            runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
            control=control,
        )

    assert error_info.value.cause.category == "interrupted"
    assert client.calls == [("category-1", 1)]
    assert database.success_calls == []
    assert database.failure_calls == []
    assert database.terminate_calls[0]["status"] == "interrupted"
    assert database.terminate_calls[0]["current_category_run_id"] == "run-1"
    assert database.terminate_calls[0]["failed_page"] == 1
    # 已验收页面仍严格完成 raw -> SQLite -> Manifest 后才响应停止。
    assert ("raw", "run-1", 1) in events
    assert ("sqlite_page", "run-1", 1) in events
    assert ("manifest", "page", "run-1", 1) in events


def test_real_sqlite_and_manifest_finish_collection_without_publication(
    tmp_path: Path,
) -> None:
    """Integrate one empty category through real runtime storage and SQLite."""

    # 隔离数据库先升级到当前全新基线 Schema。
    database_path = tmp_path / "runtime" / "data" / "collector.db"
    upgrade_database(database_path)
    database = Database(database_path)
    # 单分类发现结果模拟阶段二已经完成的真实状态。
    category = build_category(1)
    plan = CategoryRunPlan(category_run_id="real-run-1", category=category)
    discovery = CategoryDiscoveryResult(
        root_category_id="13",
        root_category_name="食品饮料",
        categories=(category,),
    )
    # 真实 BatchStorage 创建唯一 Manifest 和分页目录。
    storage = BatchStorage(
        runtime_root=tmp_path / "runtime",
        batch_id="real-stage-three-batch",
        task_id="product_hot_sale_all_level3",
        business_date=BUSINESS_DATE,
        planned_at=PLANNED_AT,
        mode="normal",
        started_at=PLANNED_AT,
    )
    try:
        database.create_batch(
            batch_id="real-stage-three-batch",
            task_id="product_hot_sale_all_level3",
            business_date=BUSINESS_DATE,
            planned_at=PLANNED_AT,
            mode="normal",
            brand_type=0,
            price_bin="10001-?",
            manifest_path=storage.manifest_path,
            started_at=PLANNED_AT,
        )
        # 分类树正文只写 runtime，再按 SQLite -> Manifest 建立索引。
        category_tree_path = storage.write_category_tree({"st": 0, "data": {}})
        database.record_category_tree_raw(
            batch_id="real-stage-three-batch",
            category_tree_raw_path=category_tree_path,
        )
        storage.record_category_tree_saved(
            category_tree_path,
            captured_at=PLANNED_AT,
        )
        database.create_category_runs(
            batch_id="real-stage-three-batch",
            discovery=discovery,
            category_run_plans=(plan,),
        )
        storage.record_discovered_categories(discovery, (plan,))
        # PreparedCategoryBatch 直接复用真实 storage 进入阶段三。
        prepared_batch = PreparedCategoryBatch(
            batch_id="real-stage-three-batch",
                task_id="product_hot_sale_all_level3",
            business_date=BUSINESS_DATE,
            planned_at=PLANNED_AT,
            mode="normal",
            started_at=PLANNED_AT,
            storage=storage,
            discovery=discovery,
            category_run_plans=(plan,),
        )
        # 空榜单仍会保存一个真实 gzip 页面并完成分类。
        result = collect_category_batch(
            prepared_batch=prepared_batch,
            task=load_task(),
            client=FakeRankingClient({"category-1": 0}),  # type: ignore[arg-type]
            database=database,
            runtime_logger=FakeRuntimeLogger(),  # type: ignore[arg-type]
        )
        with database.session_factory() as session:
            # 阶段三只完成 category_run 和 raw 索引，不进入正式发布表。
            batch = session.get(CollectionBatch, "real-stage-three-batch")
            category_run = session.get(CategoryRun, "real-run-1")
            raw_count = session.scalar(select(func.count()).select_from(RawResponse))
            product_count = session.scalar(
                select(func.count()).select_from(ProductRankEntryModel)
            )
    finally:
        database.close()

    # Manifest 必须与 SQLite 的阶段三停点一致。
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))
    assert len(result.category_runs) == 1
    assert batch is not None
    assert batch.status == "running"
    assert batch.published_at is None
    assert batch.csv_path is None
    assert category_run is not None
    assert category_run.status == "success"
    assert category_run.api_total == 0
    assert category_run.target_page_count == 1
    assert raw_count == 1
    assert product_count == 0
    assert manifest["status"] == "running"
    assert manifest["successful_category_count"] == 1
    assert manifest["saved_page_count"] == 1
    assert manifest["collected_item_count"] == 0
    assert manifest["categories"][0]["status"] == "success"
    assert (storage.categories_dir / "real-run-1" / "page-001.json.gz").is_file()
