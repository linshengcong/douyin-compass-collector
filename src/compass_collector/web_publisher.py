"""Create public website snapshots from an already published CSV."""

import csv
import gzip
from hashlib import sha256
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from compass_collector.oss_uploader import OssUploadError, OssUploader
from compass_collector.exporter import CSV_HEADERS, TAOBAO_CSV_HEADERS
from compass_collector.errors import ResponseContractError
from compass_collector.models import CollectedCategoryRun
from compass_collector.platforms.taobao_product_rank import parse_metric_value, normalize_url


# 网站对象前缀只允许安全路径段，避免环境变量改变对象层级。
WEB_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
# 网站数据版本在前端与发布器之间保持明确兼容边界。
WEB_SCHEMA_VERSION = 3
# PostgreSQL 时间是北京墙上时间，公开快照必须补充时区后再比较及展示。
PUBLIC_TIMEZONE = ZoneInfo("Asia/Shanghai")


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
        return WebPublicationSettings(
            enabled=False, error_category="web_config_invalid"
        )
    if enabled_text == "false":
        return WebPublicationSettings(enabled=False)
    public_prefix = (
        os.environ.get("WEB_PUBLIC_PREFIX", "compass/web").strip().strip("/")
    )
    if not public_prefix or WEB_PREFIX_PATTERN.fullmatch(public_prefix) is None:
        return WebPublicationSettings(enabled=True, error_category="web_config_invalid")
    # 空值只发布数据；非空静态网站地址必须是无凭据 HTTPS URL。
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


def _public_timestamp(value: datetime) -> datetime:
    """Convert PostgreSQL wall time and live aware time to the same Beijing instant."""
    return value.replace(tzinfo=PUBLIC_TIMEZONE) if value.tzinfo is None else value.astimezone(PUBLIC_TIMEZONE)


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
    def from_environment(
        cls, uploader: OssUploader, *, runtime_root: Path
    ) -> "WebPublisher":
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
        platform: str = "compass",
        update_legacy_index: bool = False,
        # 采集窗口与网页发布时间分开，历史抖音快照可没有窗口。
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        # 正式采集提供成功分类，身份字段取自原始商品而非名称或展示序号。
        category_runs: tuple[CollectedCategoryRun, ...] | None = None,
    ) -> WebPublicationResult | None:
        """Upload versioned CSV/data first and replace the public index last."""

        if not self.settings.enabled:
            return None
        if not self.settings.valid:
            raise WebPublicationError(
                self.settings.error_category or "web_config_invalid"
            )
        if not self.uploader.settings.valid:
            raise WebPublicationError("web_oss_unavailable")
        if not csv_path.is_file() or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            raise WebPublicationError("web_publication_input_invalid")

        # 发布时刻来自 PostgreSQL 快照时可能无时区；采集窗口来自内存时带时区。
        published_at = _public_timestamp(published_at)
        started_at = _public_timestamp(started_at) if started_at is not None else None
        finished_at = _public_timestamp(finished_at) if finished_at is not None else None
        if platform not in {"compass", "taobao"} or (platform == "taobao" and update_legacy_index):
            raise WebPublicationError("web_publication_input_invalid")
        if platform == "taobao" and (started_at is None or finished_at is None):
            raise WebPublicationError("web_publication_input_invalid")
        if started_at is not None or finished_at is not None:
            try:
                if started_at is None or finished_at is None or finished_at < started_at or published_at < finished_at:
                    raise WebPublicationError("web_publication_input_invalid")
            except TypeError as error:
                raise WebPublicationError("web_publication_input_invalid") from error
        # 每次发布使用独立 runtime 目录，便于排查但不进入 Git。
        # 平台及任务标识只能作为安全路径段，不能改变公开对象前缀。
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", platform) or not re.fullmatch(
            r"[a-z][a-z0-9_]*", task_id
        ):
            raise WebPublicationError("web_publication_input_invalid")
        staging_directory = (
            self.runtime_root / "web-publication" / platform / task_id / batch_id
        )
        staging_directory.mkdir(parents=True, exist_ok=True)
        data_path = staging_directory / "data.json.gz"
        index_path = staging_directory / "latest.json"
        records = _read_csv_records(csv_path, platform=platform)
        if type(item_count) is not int or item_count != len(records):
            raise WebPublicationError("web_csv_contract_invalid")
        # 只有附带可核验商品身份的新快照支持选品；旧调用仍输出只读 v3。
        schema_version = WEB_SCHEMA_VERSION
        if category_runs is not None:
            attach_selection_identity(records, category_runs, platform, batch_id)
            schema_version = 4
        # 各任务拥有独立索引，非主任务禁止覆盖旧网站根索引。
        prefix = f"{self.settings.public_prefix}/{platform}/{task_id}"
        data_key = f"{prefix}/batches/{batch_id}.json.gz"
        csv_key = f"{prefix}/batches/{batch_id}.csv"
        latest_key = f"{prefix}/latest.json"
        data_url = self.uploader.public_object_url(data_key)
        csv_url = self.uploader.public_object_url(csv_key)
        index_url = self.uploader.public_object_url(latest_key)
        # gzip 数据文件不可变，浏览器可长期缓存且始终经 latest.json 定位。
        data_payload = {
            "schema_version": schema_version,
            "batch_id": batch_id,
            "task_id": task_id,
            "platform": platform,
            "business_date": business_date.isoformat(),
            "published_at": published_at.isoformat(),
            "started_at": started_at.isoformat() if started_at is not None else None,
            "finished_at": finished_at.isoformat() if finished_at is not None else None,
            "records": records,
        }
        with gzip.open(data_path, "wt", encoding="utf-8") as file_handle:
            json.dump(
                data_payload, file_handle, ensure_ascii=False, separators=(",", ":")
            )
        # latest.json 不包含业务行，只提供当前快照元信息和公开资源 URL。
        index_payload = {
            "schema_version": schema_version,
            "batch_id": batch_id,
            "task_id": task_id,
            "platform": platform,
            "business_date": business_date.isoformat(),
            "published_at": published_at.isoformat(),
            "started_at": started_at.isoformat() if started_at is not None else None,
            "finished_at": finished_at.isoformat() if finished_at is not None else None,
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
            if update_legacy_index:
                self.uploader.upload_public_file(
                    file_path=index_path,
                    object_key=f"{self.settings.public_prefix}/latest.json",
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


def attach_selection_identity(
    records: list[dict[str, Any]],
    category_runs: tuple[CollectedCategoryRun, ...],
    platform: str,
    batch_id: str,
) -> None:
    """按 CSV 的稳定输出顺序核对原始商品，附加可提交的身份字段。"""
    # 分类发现顺序和排名排序与 CsvExporter 一致，重复商品仍有独立来源记录。
    identities = [
        (run.plan.category_run_id, run.plan.category.display_path, entry)
        for run in sorted(category_runs, key=lambda item: item.plan.category.discovery_order)
        for entry in sorted(run.entries, key=lambda item: item.rank)
    ]
    if len(identities) != len(records):
        raise WebPublicationError("web_identity_contract_invalid")
    # 逐行核对后才补身份，禁止将错位的 CSV 行绑定到另一个商品。
    for position, (row, (category_run_id, category, entry)) in enumerate(zip(records, identities)):
        if (not entry.product_id or row["category"] != category
                or row["rank"] != entry.rank or row["product_name"] != entry.product_name):
            raise WebPublicationError("web_identity_contract_invalid")
        # JSON 编码消除分隔符歧义，同批次重试得到相同来源 ID。
        identity = json.dumps([platform, batch_id, category_run_id, position, entry.product_id])
        row["product_id"] = entry.product_id
        row["source_record_id"] = sha256(identity.encode()).hexdigest()


def _read_csv_records(csv_path: Path, *, platform: str = "compass") -> list[dict[str, Any]]:
    """Convert the CSV columns into the stable public website record shape."""

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            # 严格核验列名、顺序和重复列，不能将另一平台 CSV 投影成零值。
            expected_headers = TAOBAO_CSV_HEADERS if platform == "taobao" else CSV_HEADERS
            if reader.fieldnames is None or tuple(reader.fieldnames) != expected_headers:
                raise WebPublicationError("web_csv_contract_invalid")
            records: list[dict[str, Any]] = []
            for row in reader:
                category_path = str(row.get("分类") or "")
                # 淘宝三级源名称可包含展示分隔符，前两个分隔才是分类节点边界。
                category_parts = [part.strip() for part in category_path.split(">", 2 if platform == "taobao" else -1)]
                if len(category_parts) != 3 or any(not part for part in category_parts):
                    raise WebPublicationError("web_csv_contract_invalid")
                try:
                    rank = int(str(row.get("排名") or ""))
                except ValueError as error:
                    raise WebPublicationError("web_csv_contract_invalid") from error
                if rank < 1 or None in row or any(value is None for value in row.values()):
                    raise WebPublicationError("web_csv_contract_invalid")
                # 两个平台共享展示信息；业务指标在下一步分别生成。
                record = {
                    "category": category_path,
                    "level1": category_parts[0],
                    "level2": category_parts[1],
                    "level3": category_parts[2],
                    "rank": rank,
                    "thumbnail_url": str(row.get("商品缩略图") or ""),
                    "product_name": str(row.get("商品") or ""),
                    "shop_name": str(row.get("店铺名称") or ""),
                    "platform": platform,
                }
                if platform == "taobao":
                    try:
                        # 原始指标文本与可空边界一起公开，前端不用把缺失猜成零。
                        buyers_raw, buyers = parse_metric_value(row["支付买家数"] or None)
                        visitors_raw, visitors = parse_metric_value(row["访客数"] or None)
                        product_url = normalize_url(row["商品链接"], required=True)
                        record["thumbnail_url"] = normalize_url(record["thumbnail_url"]) or ""
                    except ResponseContractError as error:
                        raise WebPublicationError("web_csv_contract_invalid") from error
                    record.update({
                        "product_url": product_url,
                        "pay_buyer_count": buyers_raw,
                        "visitor_count": visitors_raw,
                        "pay_buyer_count_min_value": str(buyers.min_value) if buyers is not None else None,
                        "visitor_count_min_value": str(visitors.min_value) if visitors is not None else None,
                        "newly_on_ranking": None,
                    })
                else:
                    if row["首次上榜"] not in {"true", "false"}:
                        raise WebPublicationError("web_csv_contract_invalid")
                    record.update({
                        "pay_amount": row["用户支付金额"],
                        "pay_combo_count": row["成交件数"],
                        "newly_on_ranking": row["首次上榜"] == "true",
                    })
                records.append(record)

    except WebPublicationError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        raise WebPublicationError("web_csv_read_failed") from error
    return records
