"""Atomic gzip page storage, run manifests, and bounded failure artifacts."""

import gzip
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


# 所有运行时时间戳使用工程确认的北京时区。
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
# 失败响应本地留档最大为 1 MiB。
MAX_FAILURE_BODY_BYTES = 1024 * 1024


def current_time_iso() -> str:
    """Return a timezone-aware ISO timestamp for manifests and diagnostics."""

    return datetime.now(SHANGHAI_TIMEZONE).isoformat()


class RunStorage:
    """Own all runtime files for one task attempt."""

    def __init__(
        self,
        runtime_root: Path,
        task_id: str,
        business_date: date,
        max_items: int,
    ) -> None:
        """Create an isolated run directory and its initial running manifest."""

        # UUID4 让同一秒内的多次调试也不会覆盖。
        self.run_id = uuid4().hex
        # 任务与业务日期用于组织运行目录和 Manifest。
        self.task_id = task_id
        self.business_date = business_date
        # 原始响应按日期、任务和 run_id 隔离。
        self.run_dir = (
            runtime_root / "raw" / business_date.isoformat() / task_id / self.run_id
        )
        self.run_dir.mkdir(parents=True, exist_ok=False)
        # 失败材料与原始成功响应使用不同目录。
        self.artifact_dir = (
            runtime_root / "artifacts" / business_date.isoformat() / self.run_id / task_id
        )
        # Manifest 在运行中通过原子替换持续更新。
        self.manifest_path = self.run_dir / "manifest.json"
        # Manifest 内容只包含脱敏运行摘要。
        self.manifest: dict[str, Any] = {
            "run_id": self.run_id,
            "task_id": task_id,
            "business_date": business_date.isoformat(),
            "status": "running",
            "page_size": 10,
            "max_items": max_items,
            "api_total": None,
            "target_items": None,
            "saved_pages": 0,
            "saved_items": 0,
            "started_at": current_time_iso(),
            "finished_at": None,
        }
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Write the current manifest through a same-directory temporary file."""

        self._write_json_atomic(self.manifest_path, self.manifest)

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        """Serialize JSON completely before atomically replacing the target."""

        # 临时文件与目标文件在同一目录，确保 os.replace 为同文件系操作。
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False, indent=2)
            file_handle.write("\n")
        os.replace(temporary_path, path)

    @staticmethod
    def _write_bytes_atomic(path: Path, payload: bytes) -> None:
        """Write bytes completely before atomically replacing the target."""

        # 失败 body 也使用同目录临时文件避免半截内容。
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("wb") as file_handle:
            file_handle.write(payload)
        os.replace(temporary_path, path)

    def write_page(self, page_no: int, payload: dict[str, Any]) -> Path:
        """Persist one validated JSON response as an atomically published gzip file."""

        # 三位页码保持原始响应的字典序排序与请求顺序一致。
        page_path = self.run_dir / f"page-{page_no:03d}.json.gz"
        # gzip 临时文件写入和关闭后才发布为正式页文件。
        temporary_path = page_path.with_name(f"{page_path.name}.tmp")
        with gzip.open(temporary_path, "wt", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary_path, page_path)
        return page_path

    def update_progress(
        self,
        *,
        api_total: int,
        target_items: int,
        saved_pages: int,
        saved_items: int,
    ) -> None:
        """Publish page-level progress after the validated gzip file exists."""

        self.manifest.update(
            {
                "api_total": api_total,
                "target_items": target_items,
                "saved_pages": saved_pages,
                "saved_items": saved_items,
            }
        )
        self._write_manifest()

    def mark_success(self) -> None:
        """Finalize the manifest only after every target page is stored."""

        self.manifest.update({"status": "success", "finished_at": current_time_iso()})
        self._write_manifest()

    def mark_failed(self, *, failed_page: int, error_category: str) -> None:
        """Finalize a failed run without adding exception text or request context."""

        self.manifest.update(
            {
                "status": "failed",
                "failed_page": failed_page,
                "error_category": error_category,
                "finished_at": current_time_iso(),
            }
        )
        self._write_manifest()

    def mark_interrupted(self, *, failed_page: int) -> None:
        """Record an explicit developer interruption as a terminal state."""

        self.manifest.update(
            {
                "status": "interrupted",
                "failed_page": failed_page,
                "error_category": "interrupted",
                "finished_at": current_time_iso(),
            }
        )
        self._write_manifest()

    def save_failure_response(
        self,
        *,
        status_code: int | None,
        error_category: str,
        response_body: bytes | None,
        failed_step: str = "http_request",
        exception_type: str = "CollectorError",
        safe_endpoint_path: str | None = None,
    ) -> None:
        """Save a bounded response body and a separate non-sensitive index."""

        # 截断标记让开发者知道本地材料不是完整 body。
        body_was_truncated = (
            response_body is not None and len(response_body) > MAX_FAILURE_BODY_BYTES
        )
        # 留档 body 最多 1 MiB，避免异常 HTML 或大响应占满磁盘。
        bounded_body = (
            response_body[:MAX_FAILURE_BODY_BYTES] if response_body is not None else None
        )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if bounded_body is not None:
            # 只有实际收到响应正文时才创建 failure-response.txt。
            response_path = self.artifact_dir / "failure-response.txt"
            self._write_bytes_atomic(response_path, bounded_body)
        # 诊断索引只记录安全分类、状态码和大小摘要。
        failure_summary = {
            "run_id": self.run_id,
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
        self._write_json_atomic(self.artifact_dir / "failure.json", failure_summary)

    def save_runtime_failure(
        self,
        *,
        error_category: str,
        failed_step: str,
        exception_type: str,
    ) -> None:
        """Save a generic safe diagnostic when no page or HTTP response exists."""

        # 运行级错误只保留稳定分类和内部步骤。
        failure_summary = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "error_category": error_category,
            "failed_step": failed_step,
            "exception_type": exception_type,
            "captured_at": current_time_iso(),
        }
        self._write_json_atomic(self.artifact_dir / "failure.json", failure_summary)

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
        """Save a safe page diagnostic and an optional atomically published PNG."""

        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if screenshot is not None:
            # Playwright 已在内存中完成 PNG 编码，存储层只负责原子发布。
            self._write_bytes_atomic(self.artifact_dir / "failure.png", screenshot)
        # 页面诊断不保存异常原文、完整 URL、HTML 或 Trace。
        failure_summary = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "error_category": error_category,
            "failed_step": failed_step,
            "exception_type": exception_type,
            "safe_page_path": safe_page_path,
            "page_title": page_title,
            "screenshot_saved": screenshot is not None,
            "captured_at": current_time_iso(),
        }
        self._write_json_atomic(self.artifact_dir / "failure.json", failure_summary)
