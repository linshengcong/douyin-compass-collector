"""Create public website snapshots from an already published CSV."""

import csv
import gzip
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from compass_collector.oss_uploader import OssUploadError, OssUploader


# 网站对象前缀只允许安全路径段，避免环境变量改变对象层级。
WEB_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
# 网站数据版本在前端与发布器之间保持明确兼容边界。
WEB_SCHEMA_VERSION = 2


class WebPublicationError(Exception):
    """Expose only one safe website-publication error category."""

    def __init__(self, category: str) -> None:
        """Keep provider and file-system details out of runtime logs."""

        super().__init__(category)
        # category 是唯一允许写入安全运行日志的诊断信息。
        self.category = category


@dataclass(frozen=True, slots=True)
class WebPublicationSettings:
    """Hold optional public website publication settings in process memory."""

    # enabled 避免现有采集在未配置网站前发生额外外网调用。
    enabled: bool
    # public_prefix 是 OSS 内唯一允许公开读取的网站对象前缀。
    public_prefix: str = "compass/web"
    # site_url 是静态托管完成后可在通知中公开展示的网站入口。
    site_url: str | None = None
    # error_category 用于把错误配置与上传失败稳定地区分开。
    error_category: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether the website publisher may write public objects."""

        return self.enabled and self.error_category is None


@dataclass(frozen=True, slots=True)
class WebPublicationResult:
    """Return public URLs only after the latest index has been replaced."""

    # index_url 是前端每次加载时读取的无缓存索引。
    index_url: str
    # data_url 指向不可变压缩数据快照，适合长期 CDN 缓存。
    data_url: str
    # csv_url 是网页按钮使用的公开 CSV 下载地址。
    csv_url: str


def load_web_publication_settings() -> WebPublicationSettings:
    """Read the opt-in website publication switch from the process environment."""

    # 不配置时保持现有 CSV 私有上传和采集行为不变。
    enabled_text = os.environ.get("WEB_ENABLED", "false").strip().lower()
    if enabled_text not in {"true", "false"}:
        return WebPublicationSettings(enabled=False, error_category="web_config_invalid")
    if enabled_text == "false":
        return WebPublicationSettings(enabled=False)
    public_prefix = os.environ.get("WEB_PUBLIC_PREFIX", "compass/web").strip().strip("/")
    if not public_prefix or WEB_PREFIX_PATTERN.fullmatch(public_prefix) is None:
        return WebPublicationSettings(enabled=True, error_category="web_config_invalid")
    # 空值保留 Vercel 旧链路；非空静态网站地址必须是无凭据 HTTPS URL。
    site_url = os.environ.get("WEB_SITE_URL", "").strip().rstrip("/") or None
    if site_url is not None and not _is_valid_public_site_url(site_url):
        return WebPublicationSettings(enabled=True, error_category="web_config_invalid")
    return WebPublicationSettings(
        enabled=True,
        public_prefix=public_prefix,
        site_url=site_url,
    )


def _is_valid_public_site_url(value: str) -> bool:
    """Accept one safe HTTPS origin for a public static website notification."""

    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.query
        and not parsed.fragment
    )


class WebPublisher:
    """Publish one immutable public data snapshot and then replace latest.json."""

    def __init__(
        self,
        settings: WebPublicationSettings,
        uploader: OssUploader,
        *,
        runtime_root: Path,
    ) -> None:
        """Bind optional website settings to the existing OSS credential boundary."""

        self.settings = settings
        self.uploader = uploader
        # runtime_root keeps derived JSON outside the repository and Git history.
        self.runtime_root = runtime_root

    @classmethod
    def from_environment(cls, uploader: OssUploader, *, runtime_root: Path) -> "WebPublisher":
        """Create a disabled-or-configured publisher without exposing credentials."""

        return cls(load_web_publication_settings(), uploader, runtime_root=runtime_root)

    def publish(
        self,
        *,
        csv_path: Path,
        task_id: str,
        batch_id: str,
        business_date: date,
        published_at: datetime,
        successful_category_count: int,
        failed_category_count: int,
        item_count: int,
    ) -> WebPublicationResult | None:
        """Upload versioned CSV/data first and replace the public index last."""

        if not self.settings.enabled:
            return None
        if not self.settings.valid:
            raise WebPublicationError(self.settings.error_category or "web_config_invalid")
        if not self.uploader.settings.valid:
            raise WebPublicationError("web_oss_unavailable")
        if not csv_path.is_file() or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            raise WebPublicationError("web_publication_input_invalid")

        # 每次发布使用独立 runtime 目录，便于排查但不进入 Git。
        staging_directory = self.runtime_root / "web-publication" / batch_id
        staging_directory.mkdir(parents=True, exist_ok=True)
        data_path = staging_directory / "data.json.gz"
        index_path = staging_directory / "latest.json"
        records = _read_csv_records(csv_path)
        data_key = f"{self.settings.public_prefix}/batches/{batch_id}.json.gz"
        csv_key = f"{self.settings.public_prefix}/batches/{batch_id}.csv"
        latest_key = f"{self.settings.public_prefix}/latest.json"
        data_url = self.uploader.public_object_url(data_key)
        csv_url = self.uploader.public_object_url(csv_key)
        index_url = self.uploader.public_object_url(latest_key)
        # gzip 数据文件不可变，浏览器可长期缓存且始终经 latest.json 定位。
        data_payload = {
            "schema_version": WEB_SCHEMA_VERSION,
            "batch_id": batch_id,
            "task_id": task_id,
            "business_date": business_date.isoformat(),
            "published_at": published_at.isoformat(),
            "records": records,
        }
        with gzip.open(data_path, "wt", encoding="utf-8") as file_handle:
            json.dump(data_payload, file_handle, ensure_ascii=False, separators=(",", ":"))
        # latest.json 不包含业务行，只提供当前快照元信息和公开资源 URL。
        index_payload = {
            "schema_version": WEB_SCHEMA_VERSION,
            "batch_id": batch_id,
            "task_id": task_id,
            "business_date": business_date.isoformat(),
            "published_at": published_at.isoformat(),
            "successful_category_count": successful_category_count,
            "failed_category_count": failed_category_count,
            "item_count": item_count,
            "data_url": data_url,
            "csv_url": csv_url,
        }
        index_path.write_text(
            json.dumps(index_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            self.uploader.upload_public_file(
                file_path=data_path,
                object_key=data_key,
                content_type="application/json",
                content_encoding="gzip",
                cache_control="public, max-age=31536000, immutable",
            )
            self.uploader.upload_public_file(
                file_path=csv_path,
                object_key=csv_key,
                content_type="text/csv; charset=utf-8",
                cache_control="public, max-age=31536000, immutable",
            )
            # 索引必须最后覆盖，防止浏览器看到指向尚未完成上传的版本。
            self.uploader.upload_public_file(
                file_path=index_path,
                object_key=latest_key,
                content_type="application/json",
                cache_control="no-store, max-age=0",
            )
        except OssUploadError as error:
            raise WebPublicationError(error.category) from None
        return WebPublicationResult(
            index_url=index_url,
            data_url=data_url,
            csv_url=csv_url,
        )


def _read_csv_records(csv_path: Path) -> list[dict[str, Any]]:
    """Convert the CSV columns into the stable public website record shape."""

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            expected_headers = {
                "分类",
                "排名",
                "商品缩略图",
                "商品",
                "店铺名称",
                "用户支付金额",
                "成交件数",
                "首次上榜",
            }
            if reader.fieldnames is None or set(reader.fieldnames) != expected_headers:
                raise WebPublicationError("web_csv_contract_invalid")
            records: list[dict[str, Any]] = []
            for row in reader:
                category_path = str(row.get("分类") or "")
                category_parts = [part.strip() for part in category_path.split(">")]
                if len(category_parts) != 3 or any(not part for part in category_parts):
                    raise WebPublicationError("web_csv_contract_invalid")
                try:
                    rank = int(str(row.get("排名") or ""))
                except ValueError as error:
                    raise WebPublicationError("web_csv_contract_invalid") from error
                records.append(
                    {
                        "category": category_path,
                        "level1": category_parts[0],
                        "level2": category_parts[1],
                        "level3": category_parts[2],
                        "rank": rank,
                        "thumbnail_url": str(row.get("商品缩略图") or ""),
                        "product_name": str(row.get("商品") or ""),
                        "shop_name": str(row.get("店铺名称") or ""),
                        "pay_amount": str(row.get("用户支付金额") or ""),
                        "pay_combo_count": str(row.get("成交件数") or ""),
                        "newly_on_ranking": str(row.get("首次上榜") or "") == "true",
                    }
                )
    except WebPublicationError:
        raise
    except OSError as error:
        raise WebPublicationError("web_csv_read_failed") from error
    return records
