"""Stage-one login and raw product-ranking collection workflows."""

import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from compass_collector.browser import BrowserSession, open_browser
from compass_collector.config import AppConfig, TaskConfig
from compass_collector.errors import AuthRequiredError, CollectorError, ResponseContractError
from compass_collector.http_client import ProductRankHttpClient
from compass_collector.product_rank import (
    PaginationPlan,
    build_request_params,
    calculate_pagination_plan,
    validate_page_payload,
)
from compass_collector.raw_storage import RunStorage


# 阶段一所有业务日期按北京时间固定。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 运行时文件统一位于仓库 runtime 目录下。
RUNTIME_ROOT = Path("runtime")


def select_tasks(config: AppConfig, selected_task_id: str | None) -> list[TaskConfig]:
    """Return enabled tasks or one explicitly selected enabled task."""

    # 启用任务是手动 run 默认的执行集合。
    enabled_tasks = [task for task in config.tasks if task.enabled]
    if selected_task_id is None:
        if not enabled_tasks:
            raise ValueError("no enabled tasks are configured")
        return enabled_tasks
    # 指定任务查找保持 CLI 行为确定。
    matching_tasks = [task for task in enabled_tasks if task.id == selected_task_id]
    if not matching_tasks:
        raise ValueError(f"enabled task not found: {selected_task_id}")
    return matching_tasks


def run_login(config: AppConfig) -> int:
    """Open the persistent profile for manual login and close on Enter."""

    # 登录命令仅管理 Chrome，不创建 HTTP 客户端。
    browser_session = open_browser(config.browser)
    try:
        browser_session.wait_for_manual_exit(
            "Chrome 已打开。完成登录和检查后，按 Enter 关闭浏览器\n"
        )
    finally:
        browser_session.close()
    return 0


def collect_task(
    task: TaskConfig,
    config: AppConfig,
    client: ProductRankHttpClient,
) -> RunStorage:
    """Collect and atomically persist all raw pages for one task."""

    # 业务日期在任务启动时只计算一次。
    business_date = datetime.now(SHANGHAI_TIMEZONE).date()
    # 每个任务尝试拥有独立的 run_id 和原始数据目录。
    storage = RunStorage(
        runtime_root=RUNTIME_ROOT,
        task_id=task.id,
        business_date=business_date,
        max_items=task.pagination.max_items,
    )
    # 首页成功后固定接口 total，后续分页不允许变化。
    expected_total: int | None = None
    # 首页返回 total 之前无法确定目标页数。
    pagination_plan: PaginationPlan | None = None
    # 当前请求从第 1 页开始串行递增。
    page_no = 1
    # 进度计数只包含已校验并原子发布的分页。
    saved_pages = 0
    saved_items = 0
    # 当前响应仅在契约失败时用于本地留档。
    current_response_body = b""
    current_status_code: int | None = None
    try:
        while pagination_plan is None or page_no <= pagination_plan.target_pages:
            # 请求参数只包含已确认的业务字段。
            params = build_request_params(task, business_date, page_no)
            # 当前页不做任何自动重试。
            page_response = client.get_page(task, params)
            current_response_body = page_response.body
            current_status_code = page_response.status_code
            # 响应在写入 gzip 前完成分页契约校验。
            page_contract = validate_page_payload(
                page_response.payload,
                requested_page=page_no,
                expected_total=expected_total,
            )
            if expected_total is None:
                expected_total = page_contract.total
                pagination_plan = calculate_pagination_plan(
                    total=expected_total,
                    max_items=task.pagination.max_items,
                )
            storage.write_page(page_no, page_response.payload)
            saved_pages += 1
            saved_items += page_contract.item_count
            storage.update_progress(
                api_total=expected_total,
                target_items=pagination_plan.target_items,
                saved_pages=saved_pages,
                saved_items=saved_items,
            )
            print(
                f"[{task.id}] 已保存第 {page_no}/{pagination_plan.target_pages} 页，"
                f"累计 {saved_items}/{pagination_plan.target_items} 条"
            )
            if page_no >= pagination_plan.target_pages:
                break
            # 正常分页请求之间使用配置范围内的随机间隔。
            delay_seconds = random.uniform(
                config.http.page_interval_seconds.min,
                config.http.page_interval_seconds.max,
            )
            print(f"[{task.id}] 等待 {delay_seconds:.2f} 秒后请求下一页")
            time.sleep(delay_seconds)
            page_no += 1
        if pagination_plan is None or saved_items != pagination_plan.target_items:
            raise ResponseContractError(
                "saved item count does not equal target items",
                category="incomplete_collection",
            )
        storage.mark_success()
        print(f"[{task.id}] 采集成功，run_id={storage.run_id}")
        return storage
    except KeyboardInterrupt:
        storage.mark_interrupted(failed_page=page_no)
        raise
    except CollectorError as error:
        # HTTP 错误自带当前 body，契约错误使用已解析的当前响应。
        failure_body = error.response_body
        if failure_body is None and isinstance(error, ResponseContractError):
            failure_body = current_response_body
        # HTTP 错误自带状态码，契约错误使用当前 2xx 状态。
        failure_status_code = error.status_code
        if failure_status_code is None and isinstance(error, ResponseContractError):
            failure_status_code = current_status_code
        if failure_body:
            storage.save_failure_response(
                status_code=failure_status_code,
                error_category=error.category,
                response_body=failure_body,
            )
        storage.mark_failed(failed_page=page_no, error_category=error.category)
        print(f"[{task.id}] 采集失败，category={error.category}")
        raise


def record_missing_auth(task: TaskConfig) -> RunStorage:
    """Create a failed run manifest when no allowlisted Cookie is available."""

    # 缺失登录态也使用当天业务日期建立可追踪尝试。
    business_date = datetime.now(SHANGHAI_TIMEZONE).date()
    # 失败 Manifest 不包含缺失的 Cookie 名称。
    storage = RunStorage(
        runtime_root=RUNTIME_ROOT,
        task_id=task.id,
        business_date=business_date,
        max_items=task.pagination.max_items,
    )
    storage.mark_failed(failed_page=1, error_category="auth_required")
    return storage


def run_collection(config: AppConfig, selected_task_id: str | None) -> int:
    """Run selected stage-one tasks in one authenticated browser lifecycle."""

    # 任务选择在启动 Chrome 前完成，避免配置错误仍打开浏览器。
    selected_tasks = select_tasks(config, selected_task_id)
    # 手动 run 的 Chrome 在本次命令中统一复用。
    browser_session: BrowserSession | None = None
    # HTTP 客户端可能在登录态检查失败前尚未创建。
    http_client: ProductRankHttpClient | None = None
    # 任何任务失败都让 CLI 返回非零状态。
    has_failures = False
    try:
        browser_session = open_browser(config.browser)
        # 运行时仅读取白名单内且对目标源适用的 Cookie。
        cookies = browser_session.whitelisted_cookies(config.auth.cookie_names)
        print(f"已从当前 Profile 读取 {len(cookies)} 个白名单 Cookie")
        if not cookies:
            record_missing_auth(selected_tasks[0])
            print("未找到可用的白名单 Cookie，请先在当前 Chrome 中登录")
            has_failures = True
        else:
            # User-Agent 从当前正式版 Chrome 动态读取。
            user_agent = browser_session.user_agent()
            http_client = ProductRankHttpClient(config.http, cookies, user_agent)
            for task in selected_tasks:
                try:
                    collect_task(task, config, http_client)
                except AuthRequiredError:
                    has_failures = True
                    print("登录态失效，本次手动运行停止后续任务")
                    break
                except CollectorError:
                    has_failures = True
                    continue
        if config.browser.keep_open_after_manual_run:
            browser_session.wait_for_manual_exit(
                "采集流程已结束。完成调试检查后，按 Enter 关闭浏览器\n"
            )
    except KeyboardInterrupt:
        has_failures = True
        print("\n已中断手动运行")
    finally:
        if http_client is not None:
            http_client.close()
        if browser_session is not None:
            browser_session.close()
    return 1 if has_failures else 0
