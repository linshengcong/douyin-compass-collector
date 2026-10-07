"""Recover today's unpublished platform evidence through the existing write pipeline."""

import gzip
import json
import zlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from compass_collector.config import TaskConfig
from compass_collector.errors import CollectionInterruptedError, ResponseContractError
from compass_collector.category_batch import PreparedCategoryBatch
from compass_collector.models import CategoryDiscoveryResult, CategoryRunPlan, CollectedCategoryRun, DiscoveredScope
from compass_collector.raw_storage import BatchStorage
from compass_collector.platforms import compass_product_rank, taobao_product_rank
from compass_collector.runtime_logging import LogContext

# 补录和平台实时榜使用同一个北京时间自然日。
TIMEZONE = ZoneInfo("Asia/Shanghai")
# 材料损坏只降级对应分类；数据库连接等运行故障不被吞掉。
EVIDENCE_ERRORS = (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error, ResponseContractError)


def _read_payload(path: Path, batch_directory: Path) -> dict:
    """只读取来源批次目录内的 gzip JSON，不允许索引越过来源边界。"""
    if not path.resolve().is_relative_to(batch_directory.resolve()):
        raise ValueError("raw page is outside source batch")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        # 解码正文只存活于当前分页的验证过程。
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("raw response must be an object")
    return payload


def _scope(category) -> DiscoveredScope:
    """从数据库冻结路径和平台参数，避免使用今天重新发现的不同分类树。"""
    return DiscoveredScope(category.discovery_order, category.category_id,
                           category.scope_path, dict(category.platform_metadata))


def _verified_entries(snapshot, category, records, task, root: Path) -> tuple:
    """完整分类验证通过后才允许跳过，禁止拼接残缺旧页与实时响应。"""
    if category.status != "success" or not records:
        raise ValueError("category is not complete")
    if len(records) != category.target_page_count or len(records) != category.saved_page_count:
        raise ValueError("raw page count mismatch")
    # 文件必须来自这个分类自己的原始目录，不能借用其他分类或尝试。
    directory = root / "raw" / snapshot.platform / snapshot.business_date.isoformat() / snapshot.task_id / snapshot.batch_id / "categories" / category.category_run_id
    # 复用各平台自己的分页、字段单位和排名契约，避免把抖音材料按淘宝格式恢复。
    parser = taobao_product_rank if task.platform == "taobao" else compass_product_rank
    # 两个平台已有的请求参数入口不同，但都能读取冻结的通用分类身份。
    build_params = parser.build_expected_params if task.platform == "taobao" else parser.build_request_params
    # 全类排名验证需要当前分类的完整记录，分类结束即释放。
    entries = []
    # 业务参数按原配置和原日期逐页核对，原始采集时间保持不变。
    scope = _scope(category)
    for number, record in enumerate(records, 1):
        if record.page_no != number or record.captured_at.date() != snapshot.business_date:
            raise ValueError("raw page date or sequence mismatch")
        if record.safe_params != build_params(task, scope, snapshot.business_date, number):
            raise ValueError("raw page scope mismatch")
        # 已完成分类的总数和目标页数同数据库状态一致才可复用。
        payload = _read_payload(record.path, directory)
        contract = parser.validate_page_payload(payload, requested_page=number, expected_total=category.api_total)
        if contract.target_page_count != category.target_page_count:
            raise ValueError("raw page plan mismatch")
        # PostgreSQL 保存的是无时区北京时间，恢复后保留其实际采集时刻。
        captured_at = record.captured_at.replace(tzinfo=TIMEZONE) if record.captured_at.tzinfo is None else record.captured_at
        # 同时校验商品字段及排名连续性，空分类也必须有合法第一页。
        page_entries = tuple(parser.parse_page_entries(payload, page_no=number, captured_at=captured_at))
        if len(page_entries) != record.item_count:
            raise ValueError("raw item count mismatch")
        entries.extend(page_entries)
    parser.validate_complete_ranking(entries, api_total=category.api_total)
    if len(entries) != category.saved_item_count:
        raise ValueError("category item count mismatch")
    return tuple(entries)


def continuation_task(snapshot) -> TaskConfig:
    """沿用原批次配置；平台和任务身份必须与数据库一致。"""
    # 黑名单是批次附加字段，不属于严格 TaskConfig 模型。
    settings = dict(snapshot.config_snapshot or {})
    settings.pop("category_blacklist", None)
    # 远程配置属于批次审计，原任务模型仍按旧契约恢复。
    settings.pop("remote_category_config", None)
    # 恢复只认可持久化的真实配置，不用当前 YAML 猜测旧范围。
    task = TaskConfig.model_validate(settings)
    if task.id != snapshot.task_id or task.platform != snapshot.platform or task.platform not in ("compass", "taobao"):
        raise ValueError("continuation task identity mismatch")
    return task


def prepare_continuation(snapshot, task, database, root, logger, control=None):
    """原批次重新执行：加载成功分类，归档其他分类旧页，再重置待采状态。"""
    # 所有页索引一次读取；正文仅按分类验证，已成功条目供现有发布流程使用。
    indexed = database.batch_raw_pages(snapshot.batch_id)
    # 分类顺序和身份完全保留，不重新请求分类树。
    plans = tuple(CategoryRunPlan(category.category_run_id, _scope(category))
                  for category in snapshot.categories)
    # 成功结果用于补录后的 CSV/数据库发布，不会重新写成功分类原始页。
    completed = []
    for category, plan in zip(snapshot.categories, plans):
        if control is not None and control.stop_requested():
            raise CollectionInterruptedError("Continuation interrupted", category="interrupted")
        if category.status != "success":
            continue
        # 验证通过才跳过，缺页/坏文件的分类从第一页重跑。
        records = indexed.get(category.category_run_id, ())
        try:
            # 只保留本分类正文，最终结果沿用原采集时间和文件索引。
            entries = _verified_entries(snapshot, category, records, task, root)
        except EVIDENCE_ERRORS:
            logger.emit(level="WARNING", event="continuation_evidence_invalid",
                        message=f"[{task.id}] 分类 {category.category_id} 材料不完整，将重新采集",
                        stage="continuation", context=LogContext(batch_id=snapshot.batch_id, task_id=task.id))
            continue
        completed.append(CollectedCategoryRun(plan, category.started_at, category.finished_at,
                         category.api_total, category.target_page_count, records,
                         entries))
    if datetime.now(TIMEZONE).date() != snapshot.business_date:
        raise CollectionInterruptedError("Business date changed", category="business_date_changed")
    # 重新打开原存储并修复 Manifest 镜像，数据库仍是权威状态。
    storage = BatchStorage.reopen(root, snapshot)
    # 只重置需要采集的分类，完整分类保持原 ID、状态和落盘文件。
    completed_ids = {run.plan.category_run_id for run in completed}
    for category in snapshot.categories:
        if category.category_run_id not in completed_ids:
            storage.archive_category_attempt(category.category_run_id, "continue")
    # 数据库事务完成后同步镜像；中间再次退出也可通过同一入口恢复。
    restarted = database.restart_unpublished_batch(snapshot.batch_id, completed_ids)
    storage.sync_collection_snapshot(restarted)
    # 让 GUI 先显示原分类总数，再逐个计入跳过的成功分类。
    logger.emit(level="INFO", event="category_discovery_succeeded",
                message=f"[{task.id}] 继续原批次，跳过 {len(completed)} 个成功分类，补录 {len(plans) - len(completed)} 个分类",
                stage="continuation", context=LogContext(batch_id=snapshot.batch_id, task_id=task.id),
                details={"discovered_category_count": len(plans)})
    for run in completed:
        logger.emit(level="INFO", event="category_collection_succeeded",
                    message=f"[{task.id}] 跳过已采集成功分类：{run.plan.category.display_path}",
                    stage="continuation", context=LogContext(batch_id=snapshot.batch_id, task_id=task.id,
                                                           category_run_id=run.plan.category_run_id),
                    details={"category_path": run.plan.category.display_path,
                             "discovery_order": run.plan.category.discovery_order,
                             "target_pages": run.target_page_count, "saved_items": run.api_total})
    if len(completed) == len(plans):
        logger.emit(level="INFO", event="continuation_publication_only",
                    message=f"[{task.id}] 分类数据完整，直接重试发布",
                    stage="continuation", context=LogContext(batch_id=snapshot.batch_id, task_id=task.id))
    return PreparedCategoryBatch(snapshot.batch_id, task.id, snapshot.business_date,
             snapshot.planned_at, snapshot.mode, snapshot.started_at, storage,
             CategoryDiscoveryResult(snapshot.root_category_id, snapshot.root_category_name,
                                     tuple(plan.category for plan in plans)), plans), tuple(completed)
