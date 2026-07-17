"""Atomic gzip storage for dynamic-category batches and diagnostics."""

import gzip
import json
import os
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence
from zoneinfo import ZoneInfo


# 所有运行时时间戳使用工程确认的北京时区。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 失败响应本地留档最大为 1 MiB。
MAX_FAILURE_BODY_BYTES = 1024 * 1024
# 阶段二只允许将批次结束为这些非发布终态。
BATCH_TERMINAL_STATUSES = {
    "failed",
    "auth_required",
    "interrupted",
    "abandoned",
}
# BatchStorage 接受的执行模式与 SQLite 约束保持一致。
BATCH_MODES = {"normal", "dry_run", "force"}


class _DiscoveredCategoryLike(Protocol):
    """Describe the category fields BatchStorage mirrors into its Manifest."""

    discovery_order: int
    level1_category_id: str
    level1_category_name: str
    level2_category_id: str
    level2_category_name: str
    category_id: str
    category_name: str


class _CategoryDiscoveryLike(Protocol):
    """Describe one parsed category scope and its ordered target categories."""

    root_category_id: str | None
    root_category_name: str | None
    categories: Sequence[_DiscoveredCategoryLike]


class _CategoryRunPlanLike(Protocol):
    """Describe the preallocated category-run identity used by all layers."""

    category_run_id: str
    category: _DiscoveredCategoryLike


class _CategoryRunSnapshotLike(Protocol):
    """Describe one authoritative category state projected into a Manifest."""

    category_run_id: str
    batch_id: str
    discovery_order: int
    level1_category_id: str
    level1_category_name: str
    level2_category_id: str
    level2_category_name: str
    category_id: str
    category_name: str
    status: str
    api_total: int | None
    target_page_count: int | None
    saved_page_count: int
    saved_item_count: int
    failed_page: int | None
    error_category: str | None
    started_at: datetime | None
    finished_at: datetime | None


class _BatchCollectionSnapshotLike(Protocol):
    """Describe the SQLite batch snapshot consumed by BatchStorage."""

    batch_id: str
    task_id: str
    business_date: date
    planned_at: datetime
    mode: str
    status: str
    version: int | None
    brand_type: int | None
    price_bin: str | None
    root_category_id: str | None
    root_category_name: str | None
    manifest_path: str | None
    category_tree_raw_path: str | None
    csv_path: str | None
    discovered_category_count: int
    successful_category_count: int
    failed_category_count: int
    not_started_category_count: int
    saved_page_count: int
    collected_item_count: int
    error_category: str | None
    started_at: datetime
    finished_at: datetime | None
    published_at: datetime | None
    categories: Sequence[_CategoryRunSnapshotLike]


def current_time_iso() -> str:
    """Return a timezone-aware ISO timestamp for manifests and diagnostics."""

    return datetime.now(SHANGHAI_TIMEZONE).isoformat()


def _manifest_time_iso(value: datetime) -> str:
    """Restore the configured timezone when SQLite returns a wall-clock value."""

    # SQLite 快照是无时区北京时间，Manifest 继续显式保留 +08:00。
    manifest_time = (
        value.replace(tzinfo=SHANGHAI_TIMEZONE)
        if value.tzinfo is None
        else value.astimezone(SHANGHAI_TIMEZONE)
    )
    return manifest_time.isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Serialize JSON completely before atomically replacing the target."""

    # 临时文件与目标文件在同一目录，确保 os.replace 为同文件系操作。
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    with temporary_path.open("w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, ensure_ascii=False, indent=2)
        file_handle.write("\n")
    os.replace(temporary_path, path)


def _write_gzip_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write one compact JSON gzip file before atomically publishing it."""

    # gzip 临时文件完整关闭后才替换正式文件，避免留下半截响应。
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(temporary_path, "wt", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary_path, path)


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write bytes completely before atomically publishing the target."""

    # 失败响应也使用同目录临时文件，避免诊断材料半写入。
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    with temporary_path.open("wb") as file_handle:
        file_handle.write(payload)
    os.replace(temporary_path, path)


def _validate_path_segment(value: str, field_name: str) -> None:
    """Reject identifiers that could escape their assigned runtime directory."""

    # 批次和分类运行 ID 只允许作为单个目录名使用。
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{field_name} must be one safe path segment")


def _category_signature(category: _DiscoveredCategoryLike) -> tuple[object, ...]:
    """Return all persisted category fields for deterministic plan comparison."""

    return (
        category.discovery_order,
        category.level1_category_id,
        category.level1_category_name,
        category.level2_category_id,
        category.level2_category_name,
        category.category_id,
        category.category_name,
    )


class BatchStorage:
    """Own the single Manifest and category-tree raw file for one batch."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        batch_id: str,
        task_id: str,
        business_date: date,
        planned_at: datetime,
        mode: Literal["normal", "dry_run", "force"],
        started_at: datetime,
    ) -> None:
        """Create the accepted batch directory and its initial running Manifest."""

        # 内部生成的批次 ID 和配置任务 ID 都必须是安全目录段。
        _validate_path_segment(batch_id, "batch_id")
        _validate_path_segment(task_id, "task_id")
        if mode not in BATCH_MODES:
            raise ValueError(f"unsupported batch mode: {mode}")
        # 批次身份由 runner 预先生成，确保文件、数据库与日志完全一致。
        self.batch_id = batch_id
        self.task_id = task_id
        self.business_date = business_date
        # 批次 raw 目录按日期、任务和 batch_id 隔离。
        self.batch_dir = (
            runtime_root / "raw" / business_date.isoformat() / task_id / batch_id
        )
        self.batch_dir.mkdir(parents=True, exist_ok=False)
        # 分类分页目录在阶段二先建立边界，但不会写入任何榜单分页。
        self.categories_dir = self.batch_dir / "categories"
        self.categories_dir.mkdir(exist_ok=False)
        # 批次级失败材料与 raw 响应分开保存。
        self.artifact_dir = (
            runtime_root
            / "artifacts"
            / business_date.isoformat()
            / task_id
            / batch_id
        )
        # 分类树和 Manifest 使用批次内固定安全文件名。
        self.category_tree_path = self.batch_dir / "category-tree.json.gz"
        self.manifest_path = self.batch_dir / "manifest.json"
        # Manifest 字段与 collection_batches/category_runs 命名保持一致。
        self.manifest: dict[str, Any] = {
            "batch_id": batch_id,
            "task_id": task_id,
            "business_date": business_date.isoformat(),
            "planned_at": planned_at.isoformat(),
            "mode": mode,
            "status": "running",
            "version": None,
            "brand_type": None,
            "price_bin": None,
            "root_category_id": None,
            "root_category_name": None,
            "manifest_path": str(self.manifest_path),
            "category_tree_raw_path": None,
            "category_tree_captured_at": None,
            "csv_path": None,
            "discovered_category_count": 0,
            "successful_category_count": 0,
            "failed_category_count": 0,
            "not_started_category_count": 0,
            "saved_page_count": 0,
            "collected_item_count": 0,
            "error_category": None,
            "started_at": started_at.isoformat(),
            "finished_at": None,
            "published_at": None,
            "categories": [],
        }
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Publish the only Manifest for this batch through atomic replacement."""

        _write_json_atomic(self.manifest_path, self.manifest)

    def _category_manifest(self, category_run_id: str) -> dict[str, Any]:
        """Return one registered category Manifest by its stable run ID."""

        # category_run_id 在进入任何路径前必须是安全单段目录名。
        _validate_path_segment(category_run_id, "category_run_id")
        # Manifest 中的分类列表是本批次允许写入分页的白名单。
        for category_manifest in self.manifest["categories"]:
            if category_manifest["category_run_id"] == category_run_id:
                return category_manifest
        raise RuntimeError("category run is not registered in this batch")

    def write_category_tree(self, payload: dict[str, Any]) -> Path:
        """Persist the complete category response exactly once."""

        if self.category_tree_path.exists():
            raise FileExistsError(f"category tree already exists: {self.category_tree_path}")
        # 本方法只发布 gzip，数据库和 Manifest 索引由编排层按顺序更新。
        _write_gzip_json_atomic(self.category_tree_path, payload)
        return self.category_tree_path

    def write_category_page(
        self,
        category_run_id: str,
        page_no: int,
        payload: dict[str, Any],
    ) -> Path:
        """Persist one ranking response below its registered category directory."""

        if page_no < 1:
            raise ValueError("page number must be positive")
        # 白名单查找同时验证 category_run_id 不能逃逸批次目录。
        self._category_manifest(category_run_id)
        # 三位页码让文件字典序与真实请求顺序保持一致。
        category_page_path = (
            self.categories_dir
            / category_run_id
            / f"page-{page_no:03d}.json.gz"
        )
        if category_page_path.exists():
            raise FileExistsError(f"category page already exists: {category_page_path}")
        # gzip 完整关闭后才原子发布，数据库随后才能登记该路径。
        _write_gzip_json_atomic(category_page_path, payload)
        return category_page_path

    def record_category_tree_saved(
        self,
        category_tree_path: Path,
        *,
        captured_at: datetime,
    ) -> None:
        """Index an already-published category-tree file in the Manifest."""

        if self.manifest["status"] != "running":
            raise RuntimeError("cannot record a category tree for a terminal batch")
        if self.manifest["category_tree_raw_path"] is not None:
            raise RuntimeError("category tree has already been recorded")
        if category_tree_path != self.category_tree_path:
            raise ValueError("category tree path does not belong to this batch")
        if not category_tree_path.is_file():
            raise FileNotFoundError(category_tree_path)
        # Manifest 只在正式 gzip 已存在且数据库已经接纳路径后更新。
        self.manifest["category_tree_raw_path"] = str(category_tree_path)
        self.manifest["category_tree_captured_at"] = captured_at.isoformat()
        self._write_manifest()

    def record_discovered_categories(
        self,
        discovery: _CategoryDiscoveryLike,
        category_run_plans: Sequence[_CategoryRunPlanLike],
    ) -> None:
        """Record one non-empty ordered discovery result in the batch Manifest."""

        if self.manifest["status"] != "running":
            raise RuntimeError("cannot record categories for a terminal batch")
        if self.manifest["category_tree_raw_path"] is None:
            raise RuntimeError("category tree must be stored before category discovery")
        if self.manifest["categories"]:
            raise RuntimeError("categories have already been recorded")
        # 元组快照避免调用方在校验过程中修改输入序列。
        discovered_categories = tuple(discovery.categories)
        planned_category_runs = tuple(category_run_plans)
        if not discovered_categories:
            raise ValueError("category discovery must contain at least one category")
        if len(discovered_categories) != len(planned_category_runs):
            raise ValueError("category run plans must match discovered categories")
        if (discovery.root_category_id is None) != (
            discovery.root_category_name is None
        ):
            raise ValueError("category discovery root fields must be both set or both null")
        # 批次内分类 ID 与 category_run_id 都必须唯一。
        category_ids: set[str] = set()
        category_run_ids: set[str] = set()
        # 完整校验后一次性替换 categories，避免 Manifest 出现部分分类。
        category_manifests: list[dict[str, Any]] = []
        for expected_order, (category, plan) in enumerate(
            zip(discovered_categories, planned_category_runs, strict=True),
            start=1,
        ):
            if category.discovery_order != expected_order:
                raise ValueError("category discovery order must be continuous from one")
            if _category_signature(plan.category) != _category_signature(category):
                raise ValueError("category run plan does not match discovery result")
            if discovery.root_category_id is not None and (
                category.level1_category_id != discovery.root_category_id
                or category.level1_category_name != discovery.root_category_name
            ):
                raise ValueError("category root does not match discovery root")
            _validate_path_segment(plan.category_run_id, "category_run_id")
            if category.category_id in category_ids:
                raise ValueError("duplicate category_id in one batch")
            if plan.category_run_id in category_run_ids:
                raise ValueError("duplicate category_run_id in one batch")
            category_ids.add(category.category_id)
            category_run_ids.add(plan.category_run_id)
            # 分类 Manifest 与 category_runs 表字段逐项对应。
            category_manifests.append(
                {
                    "category_run_id": plan.category_run_id,
                    "discovery_order": category.discovery_order,
                    "level1_category_id": category.level1_category_id,
                    "level1_category_name": category.level1_category_name,
                    "level2_category_id": category.level2_category_id,
                    "level2_category_name": category.level2_category_name,
                    "category_id": category.category_id,
                    "category_name": category.category_name,
                    "status": "pending",
                    "api_total": None,
                    "target_page_count": None,
                    "saved_page_count": 0,
                    "saved_item_count": 0,
                    "failed_page": None,
                    "error_category": None,
                    "started_at": None,
                    "finished_at": None,
                }
            )
        self.manifest.update(
            {
                "root_category_id": discovery.root_category_id,
                "root_category_name": discovery.root_category_name,
                "discovered_category_count": len(category_manifests),
                "categories": category_manifests,
            }
        )
        self._write_manifest()

    def sync_collection_snapshot(
        self,
        snapshot: _BatchCollectionSnapshotLike,
    ) -> None:
        """Atomically replace collection state from one authoritative SQLite snapshot."""

        # 存储实例与数据库快照必须描述同一个顶层批次。
        if snapshot.batch_id != self.batch_id:
            raise ValueError("snapshot batch id does not match storage")
        if snapshot.task_id != self.task_id:
            raise ValueError("snapshot task id does not match storage")
        if snapshot.business_date != self.business_date:
            raise ValueError("snapshot business date does not match storage")
        if snapshot.mode != self.manifest["mode"]:
            raise ValueError("snapshot mode does not match storage")
        if snapshot.manifest_path != str(self.manifest_path):
            raise ValueError("snapshot manifest path does not match storage")
        if (
            snapshot.category_tree_raw_path is not None
            and snapshot.category_tree_raw_path != str(self.category_tree_path)
        ):
            raise ValueError("snapshot category-tree path does not match storage")
        if (snapshot.root_category_id is None) != (
            snapshot.root_category_name is None
        ):
            raise ValueError("snapshot root category id and name must appear together")
        # 元组冻结调用方序列，确保一次同步过程读取稳定分类集合。
        category_snapshots = tuple(snapshot.categories)
        if snapshot.discovered_category_count != len(category_snapshots):
            raise ValueError("snapshot category count is inconsistent")
        # 已登记 Manifest 分类用于防止 SQLite 快照意外改变分类身份。
        existing_categories_by_id = {
            category_manifest["category_run_id"]: category_manifest
            for category_manifest in self.manifest["categories"]
        }
        # 完整校验并构造后再执行一次原子替换。
        category_manifests: list[dict[str, Any]] = []
        # category_run_id 不得在一个快照中重复出现。
        seen_category_run_ids: set[str] = set()
        for expected_order, category_snapshot in enumerate(
            category_snapshots,
            start=1,
        ):
            _validate_path_segment(
                category_snapshot.category_run_id,
                "category_run_id",
            )
            if category_snapshot.category_run_id in seen_category_run_ids:
                raise ValueError("snapshot contains duplicate category run ids")
            seen_category_run_ids.add(category_snapshot.category_run_id)
            if category_snapshot.batch_id != self.batch_id:
                raise ValueError("snapshot category belongs to another batch")
            if category_snapshot.discovery_order != expected_order:
                raise ValueError("snapshot category order must be continuous from one")
            # 阶段二已登记的分类路径不允许在采集过程中被改写。
            existing_category = existing_categories_by_id.get(
                category_snapshot.category_run_id
            )
            if existing_category is not None:
                existing_signature = (
                    existing_category["discovery_order"],
                    existing_category["level1_category_id"],
                    existing_category["level1_category_name"],
                    existing_category["level2_category_id"],
                    existing_category["level2_category_name"],
                    existing_category["category_id"],
                    existing_category["category_name"],
                )
                snapshot_signature = (
                    category_snapshot.discovery_order,
                    category_snapshot.level1_category_id,
                    category_snapshot.level1_category_name,
                    category_snapshot.level2_category_id,
                    category_snapshot.level2_category_name,
                    category_snapshot.category_id,
                    category_snapshot.category_name,
                )
                if existing_signature != snapshot_signature:
                    raise ValueError("snapshot category identity changed")
            # 分类运行字段与 CategoryRunSnapshot 一一对应。
            category_manifests.append(
                {
                    "category_run_id": category_snapshot.category_run_id,
                    "discovery_order": category_snapshot.discovery_order,
                    "level1_category_id": category_snapshot.level1_category_id,
                    "level1_category_name": category_snapshot.level1_category_name,
                    "level2_category_id": category_snapshot.level2_category_id,
                    "level2_category_name": category_snapshot.level2_category_name,
                    "category_id": category_snapshot.category_id,
                    "category_name": category_snapshot.category_name,
                    "status": category_snapshot.status,
                    "api_total": category_snapshot.api_total,
                    "target_page_count": category_snapshot.target_page_count,
                    "saved_page_count": category_snapshot.saved_page_count,
                    "saved_item_count": category_snapshot.saved_item_count,
                    "failed_page": category_snapshot.failed_page,
                    "error_category": category_snapshot.error_category,
                    "started_at": (
                        _manifest_time_iso(category_snapshot.started_at)
                        if category_snapshot.started_at is not None
                        else None
                    ),
                    "finished_at": (
                        _manifest_time_iso(category_snapshot.finished_at)
                        if category_snapshot.finished_at is not None
                        else None
                    ),
                }
            )
        if existing_categories_by_id and set(existing_categories_by_id) != (
            seen_category_run_ids
        ):
            raise ValueError("snapshot category set does not match Manifest")
        # category_tree_captured_at 没有数据库列，继续保留阶段二已记录值。
        category_tree_captured_at = self.manifest["category_tree_captured_at"]
        # 其余状态全部由 SQLite 快照重新投影，不进行增量合并。
        updated_manifest: dict[str, Any] = {
            "batch_id": snapshot.batch_id,
            "task_id": snapshot.task_id,
            "business_date": snapshot.business_date.isoformat(),
            "planned_at": _manifest_time_iso(snapshot.planned_at),
            "mode": snapshot.mode,
            "status": snapshot.status,
            "version": snapshot.version,
            "brand_type": snapshot.brand_type,
            "price_bin": snapshot.price_bin,
            "root_category_id": snapshot.root_category_id,
            "root_category_name": snapshot.root_category_name,
            "manifest_path": snapshot.manifest_path,
            "category_tree_raw_path": snapshot.category_tree_raw_path,
            "category_tree_captured_at": category_tree_captured_at,
            "csv_path": snapshot.csv_path,
            "discovered_category_count": snapshot.discovered_category_count,
            "successful_category_count": snapshot.successful_category_count,
            "failed_category_count": snapshot.failed_category_count,
            "not_started_category_count": snapshot.not_started_category_count,
            "saved_page_count": snapshot.saved_page_count,
            "collected_item_count": snapshot.collected_item_count,
            "error_category": snapshot.error_category,
            "started_at": _manifest_time_iso(snapshot.started_at),
            "finished_at": (
                _manifest_time_iso(snapshot.finished_at)
                if snapshot.finished_at is not None
                else None
            ),
            "published_at": (
                _manifest_time_iso(snapshot.published_at)
                if snapshot.published_at is not None
                else None
            ),
            "categories": category_manifests,
        }
        _write_json_atomic(self.manifest_path, updated_manifest)
        # 只有正式文件发布成功后才替换内存镜像，失败时可复用同一快照重试。
        self.manifest = updated_manifest

    def mark_batch_terminal(
        self,
        *,
        status: Literal["failed", "auth_required", "interrupted", "abandoned"],
        error_category: str,
        finished_at: datetime,
        root_category_id: str | None = None,
        root_category_name: str | None = None,
    ) -> None:
        """Finalize a non-published batch, including a zero-category discovery."""

        if self.manifest["status"] != "running":
            raise RuntimeError("batch is already terminal")
        if status not in BATCH_TERMINAL_STATUSES:
            raise ValueError(f"unsupported terminal batch status: {status}")
        if not error_category:
            raise ValueError("terminal batch requires an error_category")
        if (root_category_id is None) != (root_category_name is None):
            raise ValueError("root category id and name must be provided together")
        # 终态先在副本中构造，写入失败时内存仍保持 running 以允许重试。
        updated_manifest = deepcopy(self.manifest)
        # 已发现根分类但三级分类为空时仍保留当次根分类快照。
        if root_category_id is not None and root_category_name is not None:
            existing_root_id = updated_manifest["root_category_id"]
            existing_root_name = updated_manifest["root_category_name"]
            if existing_root_id not in {None, root_category_id} or existing_root_name not in {
                None,
                root_category_name,
            }:
                raise ValueError("terminal root category conflicts with Manifest")
            updated_manifest["root_category_id"] = root_category_id
            updated_manifest["root_category_name"] = root_category_name
        # 已经登记但尚未开始的分类必须随批次一起收口。
        pending_category_count = 0
        for category_manifest in updated_manifest["categories"]:
            if category_manifest["status"] != "pending":
                continue
            category_manifest["status"] = "not_started"
            category_manifest["finished_at"] = finished_at.isoformat()
            pending_category_count += 1
        updated_manifest.update(
            {
                "status": status,
                "not_started_category_count": pending_category_count,
                "error_category": error_category,
                "finished_at": finished_at.isoformat(),
            }
        )
        _write_json_atomic(self.manifest_path, updated_manifest)
        # 只有正式 Manifest 发布成功后才替换内存状态。
        self.manifest = updated_manifest

    def save_category_failure(
        self,
        *,
        category_run_id: str,
        failed_page: int | None,
        status_code: int | None,
        error_category: str,
        response_body: bytes | None,
        failed_step: str,
        exception_type: str,
        safe_endpoint_path: str,
    ) -> None:
        """Save bounded diagnostic material under one category-run artifact path."""

        # 只允许为阶段二已登记的分类创建故障材料目录。
        self._category_manifest(category_run_id)
        if failed_page is not None and failed_page < 1:
            raise ValueError("failed page must be positive")
        if not error_category:
            raise ValueError("category failure requires an error category")
        # 分类级目录避免多个失败分类互相覆盖 failure.json。
        category_artifact_dir = self.artifact_dir / category_run_id
        category_artifact_dir.mkdir(parents=True, exist_ok=True)
        # 正文最多保留 1 MiB，避免异常 HTML 或网关响应占满磁盘。
        bounded_body = (
            response_body[:MAX_FAILURE_BODY_BYTES]
            if response_body is not None
            else None
        )
        # 截断标记供后续人工诊断判断材料是否完整。
        body_was_truncated = (
            response_body is not None and len(response_body) > MAX_FAILURE_BODY_BYTES
        )
        if bounded_body is not None:
            # 响应正文只保存在 runtime/artifacts，不写入日志或 Manifest。
            _write_bytes_atomic(
                category_artifact_dir / "failure-response.txt",
                bounded_body,
            )
        # 安全索引只包含稳定错误分类、固定接口路径和大小信息。
        failure_summary = {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "category_run_id": category_run_id,
            "failed_page": failed_page,
            "status_code": status_code,
            "error_category": error_category,
            "failed_step": failed_step,
            "exception_type": exception_type,
            "safe_endpoint_path": safe_endpoint_path,
            "response_saved": bounded_body is not None,
            "saved_bytes": len(bounded_body) if bounded_body is not None else 0,
            "truncated": body_was_truncated,
            "captured_at": current_time_iso(),
        }
        _write_json_atomic(category_artifact_dir / "failure.json", failure_summary)

    def save_failure(
        self,
        *,
        status_code: int | None,
        error_category: str,
        response_body: bytes | None,
        failed_step: str,
        exception_type: str,
        safe_endpoint_path: str,
    ) -> None:
        """Save one bounded local response and a sanitized batch failure index."""

        # 失败正文最多保留 1 MiB，防止异常页面占满本地磁盘。
        bounded_body = (
            response_body[:MAX_FAILURE_BODY_BYTES]
            if response_body is not None
            else None
        )
        # 截断标记明确说明本地材料是否完整。
        body_was_truncated = (
            response_body is not None and len(response_body) > MAX_FAILURE_BODY_BYTES
        )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if bounded_body is not None:
            # 正文只进入 runtime/artifacts，不复制到日志或 Manifest。
            _write_bytes_atomic(
                self.artifact_dir / "failure-response.txt",
                bounded_body,
            )
        # failure.json 只记录安全分类、大小和固定接口路径。
        failure_summary = {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "status_code": status_code,
            "error_category": error_category,
            "failed_step": failed_step,
            "exception_type": exception_type,
            "safe_endpoint_path": safe_endpoint_path,
            "response_saved": bounded_body is not None,
            "saved_bytes": len(bounded_body) if bounded_body is not None else 0,
            "truncated": body_was_truncated,
            "captured_at": current_time_iso(),
        }
        _write_json_atomic(self.artifact_dir / "failure.json", failure_summary)

    def save_browser_failure(
        self,
        *,
        error_category: str,
        failed_step: str,
        exception_type: str,
        safe_page_path: str | None,
        page_title: str | None,
        screenshot: bytes | None,
    ) -> None:
        """Save safe page metadata and an optional atomically published screenshot."""

        if not error_category:
            raise ValueError("browser failure requires an error category")
        if safe_page_path is not None and (
            not safe_page_path.startswith("/")
            or "?" in safe_page_path
            or "#" in safe_page_path
        ):
            raise ValueError("browser failure requires a safe page path")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if screenshot is not None:
            # Playwright 已在内存中完成 PNG 编码，BatchStorage 只负责原子发布。
            _write_bytes_atomic(self.artifact_dir / "failure.png", screenshot)
        # 页面标题由错误边界截断；存储层再次限制长度防止其他调用方绕过。
        safe_page_title = page_title[:200] if page_title else None
        # 页面诊断不保存异常原文、完整 URL、HTML、Cookie 或 Trace。
        failure_summary = {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "error_category": error_category,
            "failed_step": failed_step,
            "exception_type": exception_type,
            "safe_page_path": safe_page_path,
            "page_title": safe_page_title,
            "screenshot_saved": screenshot is not None,
            "captured_at": current_time_iso(),
        }
        _write_json_atomic(self.artifact_dir / "failure.json", failure_summary)
