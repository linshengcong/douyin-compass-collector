"""Prepare one dynamic-category batch before any ranking page is requested."""

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from compass_collector.config import TaskConfig
from compass_collector.errors import (
    AuthRequiredError,
    CategoryBatchPreparationError,
    CategoryDiscoveryEmptyError,
    CollectionInterruptedError,
    CollectorError,
)
from compass_collector.platforms.contracts import PlatformAdapter
from compass_collector.models import (
    CategoryDiscoveryResult,
    CategoryRunPlan,
)
from compass_collector.persistence import Database
from compass_collector.raw_storage import BatchStorage
from compass_collector.run_control import CollectionControl
from compass_collector.runtime_logging import LogContext, RuntimeLogger


# 分类发现和数据库时间统一使用北京时间。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 阶段二批次模式与配置和 PostgreSQL 约束保持一致。
BatchMode = Literal["normal", "dry_run", "force"]
# 分类发现阶段只会进入这三个失败终态。
BatchFailureStatus = Literal["failed", "auth_required", "interrupted"]


@dataclass(frozen=True, slots=True)
class PreparedCategoryBatch:
    """Expose the stage-two state required by the later ranking loop."""

    # batch_id 连接 Manifest、PostgreSQL、raw 目录和日志。
    batch_id: str
    task_id: str
    business_date: date
    planned_at: datetime
    mode: BatchMode
    started_at: datetime
    storage: BatchStorage
    discovery: CategoryDiscoveryResult
    category_run_plans: tuple[CategoryRunPlan, ...]


def _terminal_status(error: CollectorError) -> BatchFailureStatus:
    """Map one safe collector error to the batch lifecycle status."""

    if isinstance(error, AuthRequiredError):
        return "auth_required"
    if isinstance(error, CollectionInterruptedError):
        return "interrupted"
    return "failed"


def _finish_failed_batch(
    *,
    database: Database,
    storage: BatchStorage,
    runtime_logger: RuntimeLogger,
    task_id: str,
    error: CollectorError,
    exception_type: str,
) -> None:
    """Persist one discovery failure consistently in PostgreSQL, Manifest, and logs."""

    # 同一个完成时刻写入数据库和 Manifest，避免诊断时间漂移。
    finished_at = datetime.now(SHANGHAI_TIMEZONE)
    # 鉴权和人工中止保留独立终态，其他错误统一为 failed。
    status = _terminal_status(error)
    # 空分类错误仍保留已经唯一定位成功的根节点。
    root_category_id = (
        error.root_category_id
        if isinstance(error, CategoryDiscoveryEmptyError)
        else None
    )
    # 根名称与根 ID 成对写入数据库和 Manifest。
    root_category_name = (
        error.root_category_name
        if isinstance(error, CategoryDiscoveryEmptyError)
        else None
    )
    try:
        database.finish_discovery_failure(
            batch_id=storage.batch_id,
            status=status,
            error_category=error.category,
            finished_at=finished_at,
            root_category_id=root_category_id,
            root_category_name=root_category_name,
        )
    except Exception:
        # PostgreSQL 不可写时仍尝试终止 Manifest，差异由后续恢复流程处理。
        pass
    # Manifest 是 PostgreSQL 的审计镜像，瞬时写入失败时原位重试一次。
    for manifest_attempt in range(2):
        try:
            storage.mark_batch_terminal(
                status=status,
                error_category=error.category,
                finished_at=finished_at,
                root_category_id=root_category_id,
                root_category_name=root_category_name,
            )
            break
        except OSError:
            if manifest_attempt == 1:
                # PostgreSQL 已是权威终态；后续恢复需据此重建 Manifest。
                break
    try:
        # 失败正文最多 1 MiB 且只写入 runtime/artifacts。
        storage.save_failure(
            status_code=error.status_code,
            error_category=error.category,
            response_body=error.response_body,
            failed_step="category_tree_request_or_contract",
            exception_type=exception_type,
            safe_endpoint_path=None,
        )
    except OSError:
        # 诊断材料写入失败不能覆盖已经持久化的业务终态。
        pass
    try:
        runtime_logger.emit(
            level="WARNING" if status == "interrupted" else "ERROR",
            event="category_discovery_failed",
            message=f"[{task_id}] 分类发现失败，category={error.category}",
            stage="category_discovery",
            context=LogContext(batch_id=storage.batch_id, task_id=task_id),
            details={
                "batch_status": status,
                "error_category": error.category,
                # 仅保留异常类型，便于定位且不暴露底层异常正文。
                "exception_type": exception_type,
                "status_code": error.status_code,
            },
        )
    except Exception:
        # 日志不可用不能覆盖已经形成的业务终态和原始错误。
        pass


def prepare_category_batch(
    *,
    runtime_root: Path,
    batch_id: str,
    task: TaskConfig,
    business_date: date,
    planned_at: datetime,
    mode: BatchMode,
    client: PlatformAdapter,
    database: Database,
    runtime_logger: RuntimeLogger,
    control: CollectionControl | None = None,
    category_blacklist: tuple[str, ...] = (),
    # remote 在新批次内部读取；补录不会经过本方法。
    category_config_source: str = "yaml",
) -> PreparedCategoryBatch:
    """Request one category tree and create all pending level-three runs."""

    # 批次开始时间在创建 Manifest 和 PostgreSQL 行之前只计算一次。
    started_at = datetime.now(SHANGHAI_TIMEZONE)
    # 平台黑名单随批次固定，历史审计不依赖之后修改的配置文件。
    config_snapshot = {
        **task.model_dump(mode="json"),
        "category_blacklist": list(category_blacklist) if category_config_source == "yaml" else [],
    }
    # BatchStorage 初始化只建立批次目录和单一运行中 Manifest。
    storage = BatchStorage(
        runtime_root=runtime_root,
        batch_id=batch_id,
        task_id=task.id,
        business_date=business_date,
        planned_at=planned_at,
        mode=mode,
        started_at=started_at,
        platform=task.platform,
        config_snapshot=config_snapshot,
    )
    try:
        database.create_batch(
            batch_id=batch_id,
            task_id=task.id,
            business_date=business_date,
            planned_at=planned_at,
            mode=mode,
            brand_type=task.filters.brand_type if task.platform == "compass" else None,
            price_bin=task.filters.price_bin if task.platform == "compass" else None,
            manifest_path=storage.manifest_path,
            started_at=started_at,
            platform=task.platform,
            config_snapshot=config_snapshot,
        )
    except Exception as error:
        # 数据库批次未创建时只能终止 Manifest，不能伪造 PostgreSQL 终态。
        safe_error = CollectorError(
            "Could not create the category batch",
            category="database_error",
        )
        storage.mark_batch_terminal(
            status="failed",
            error_category=safe_error.category,
            finished_at=datetime.now(SHANGHAI_TIMEZONE),
        )
        raise CategoryBatchPreparationError(safe_error, storage) from error

    # 批次级日志在任何网络请求前建立稳定关联。
    batch_context = LogContext(batch_id=batch_id, task_id=task.id)
    try:
        runtime_logger.emit(
            level="INFO",
            event="category_batch_started",
            message=f"[{task.id}] 开始请求分类树",
            stage="category_discovery",
            context=batch_context,
            details={"planned_at": planned_at.isoformat()},
        )
        if control is not None and control.stop_requested():
            raise CollectionInterruptedError(
                "Collection interrupted before category discovery",
                category="interrupted",
            )
        # 分类接口只使用三个已确认的固定业务参数。
        # 先读取并固定规则，再允许平台浏览器打开；配置失败记录批次失败。
        remote_config = None
        if category_config_source == "remote":
            from compass_collector.remote_categories import fetch_category_config, retry_catalog
            remote_config = fetch_category_config(task.platform, task.id)
            retry_catalog(task.platform, runtime_root, runtime_logger)
            config_snapshot["remote_category_config"] = remote_config
            database.record_category_config(batch_id=batch_id, config_snapshot=config_snapshot)
            storage.record_category_config(config_snapshot)
        category_response = client.discover_scopes(task, full_catalog=True) if remote_config else client.discover_scopes(task)
        # 完整响应只写入 runtime，不进入仓库 Fixture、日志或 Manifest。
        category_tree_path = storage.write_category_tree(category_response.payload)
        database.record_category_tree_raw(
            batch_id=batch_id,
            category_tree_raw_path=category_tree_path,
        )
        # Manifest 只在数据库已经记录正式 gzip 路径后建立索引。
        storage.record_category_tree_saved(
            category_tree_path,
            captured_at=datetime.now(SHANGHAI_TIMEZONE),
        )
        if control is not None and control.stop_requested():
            raise CollectionInterruptedError(
                "Collection interrupted after category discovery request",
                category="interrupted",
            )
        # 所有一级分类和目标三级分类只从当次 data.cate_list 动态解析。
        discovery = category_response.discovery
        if remote_config:
            from compass_collector.category_catalog import catalog_from_payload, scope_path
            from compass_collector.category_rules import preview_rules, catalog_digest
            from compass_collector.remote_categories import upload_catalog
            # 全目录与原响应保存在同一批次，传输失败仅保留待同步标记。
            nodes, full_discovery = catalog_from_payload(task.platform, category_response.payload)
            (storage.batch_dir / "category-catalog.json").write_text(json.dumps(nodes, ensure_ascii=False), encoding="utf-8")
            upload_catalog(task.platform, nodes, datetime.now(SHANGHAI_TIMEZONE), runtime_root, runtime_logger)
            try:
                # 校验依赖当次目录，不能依赖接口返回的旧目录猜测缺失分类。
                preview = preview_rules(nodes, remote_config["rules"], task.id)
            except ValueError as error:
                raise CollectorError("远程分类规则无效", category="category_remote_invalid") from error
            if not preview["valid"]:
                raise CollectorError("远程分类规则与当次目录不一致或没有可采分类", category="category_remote_scope_invalid")
            # 完整 ID 路径过滤不改变实际平台请求元数据。
            scopes_by_path = {tuple(scope_path(scope)): scope for scope in full_discovery.categories}
            discovery = replace(full_discovery, categories=tuple(
                replace(scopes_by_path[tuple(item["path"])], discovery_order=index)
                for index, item in enumerate(preview["included"], 1)))
            remote_config["catalog_version"] = catalog_digest(nodes)
            remote_config["effective_paths"] = [item["path"] for item in preview["included"]]
            database.record_category_config(batch_id=batch_id, config_snapshot=config_snapshot)
            storage.record_category_config(config_snapshot)
        # 路径中任一层级完整名称命中即跳过，不按子串或另一个平台名单过滤。
        blocked_names = {name.strip() for name in category_blacklist} if not remote_config else set()
        # 排除项没有 category_run，因此既不进入采集，也不进入失败补采。
        retained_categories = []
        for category in discovery.categories:
            if any(name.strip() in blocked_names for name in category.path):
                runtime_logger.emit(
                    level="INFO",
                    event="category_blacklisted",
                    message=(
                        f"[{task.platform}/{task.id}] 黑名单跳过："
                        f"{category.display_path}"
                    ),
                    stage="category_discovery",
                    context=batch_context,
                    details={
                        "category_id": category.category_id,
                        "category_path": category.display_path,
                        "discovery_order": category.discovery_order,
                        "error_category": "category_blacklisted",
                    },
                )
            else:
                retained_categories.append(
                    replace(category, discovery_order=len(retained_categories) + 1)
                )
        if discovery.categories and not retained_categories:
            raise CollectorError(
                "所有分类均命中当前平台黑名单，无可采分类",
                category="category_blacklist_empty",
            )
        discovery = replace(discovery, categories=tuple(retained_categories))
        # 一级分类数量从发现结果去重得到，不制造虚假的批次根节点。
        level1_category_count = len(
            {category.level1_category_id for category in discovery.categories}
        )
        # 每个分类运行 ID 在数据库和 Manifest 写入之前一次性生成。
        category_run_plans = tuple(
            CategoryRunPlan(category_run_id=uuid4().hex, category=category)
            for category in discovery.categories
        )
        database.create_category_runs(
            batch_id=batch_id,
            discovery=discovery,
            category_run_plans=category_run_plans,
        )
        storage.record_discovered_categories(discovery, category_run_plans)
        runtime_logger.emit(
            level="INFO",
            event="category_discovery_succeeded",
            message=(
                f"[{task.id}] 已发现 {level1_category_count} 个一级分类、"
                f"{len(discovery.categories)} 个三级分类，"
                "尚未请求榜单分页"
            ),
            stage="category_discovery",
            context=batch_context,
            details={
                "discovered_category_count": len(discovery.categories),
                "level1_category_count": level1_category_count,
            },
        )
        for plan in category_run_plans:
            # 每个分类单独打印，便于正式循环前人工核对顺序和路径。
            category = plan.category
            runtime_logger.emit(
                level="INFO",
                event="category_discovered",
                message=f"{category.discovery_order:03d} {category.display_path}",
                stage="category_discovery",
                context=LogContext(
                    batch_id=batch_id,
                    task_id=task.id,
                    category_run_id=plan.category_run_id,
                ),
                details={
                    "category_id": category.category_id,
                    "category_path": category.display_path,
                    "discovery_order": category.discovery_order,
                },
            )
        return PreparedCategoryBatch(
            batch_id=batch_id,
            task_id=task.id,
            business_date=business_date,
            planned_at=planned_at,
            mode=mode,
            started_at=started_at,
            storage=storage,
            discovery=discovery,
            category_run_plans=category_run_plans,
        )
    except CollectorError as error:
        # 适配器范围校验失败仍保留当次树，禁止创建任何待采分类。
        if error.discovery_payload is not None:
            category_tree_path = storage.write_category_tree(error.discovery_payload)
            database.record_category_tree_raw(
                batch_id=batch_id, category_tree_raw_path=category_tree_path
            )
            storage.record_category_tree_saved(
                category_tree_path, captured_at=datetime.now(SHANGHAI_TIMEZONE)
            )
        _finish_failed_batch(
            database=database,
            storage=storage,
            runtime_logger=runtime_logger,
            task_id=task.id,
            error=error,
            exception_type=type(error).__name__,
        )
        raise CategoryBatchPreparationError(error, storage) from error
    except Exception as error:
        # 未预期异常只转换为稳定分类，异常原文不会进入日志或 Manifest。
        safe_error = CollectorError(
            "Unexpected category batch failure",
            category="internal_error",
        )
        _finish_failed_batch(
            database=database,
            storage=storage,
            runtime_logger=runtime_logger,
            task_id=task.id,
            error=safe_error,
            exception_type=type(error).__name__,
        )
        raise CategoryBatchPreparationError(safe_error, storage) from error
    except (KeyboardInterrupt, SystemExit) as error:
        # 进程级中止必须先收口已创建的 PostgreSQL 批次和 Manifest。
        interruption_error = CollectionInterruptedError(
            "Category discovery interrupted",
            category="interrupted",
        )
        _finish_failed_batch(
            database=database,
            storage=storage,
            runtime_logger=runtime_logger,
            task_id=task.id,
            error=interruption_error,
            exception_type=type(error).__name__,
        )
        # 保留 KeyboardInterrupt/SystemExit 原始语义交给顶层生命周期处理。
        raise
