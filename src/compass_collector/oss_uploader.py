"""Private OSS CSV uploads and seven-day signed download links."""

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


# OSS V4 签名下载链接的官方最长有效期为七天。
MAX_DOWNLOAD_URL_EXPIRES_SECONDS = 7 * 24 * 60 * 60
# Bucket 与对象前缀仅接受可预测的安全字符，避免配置形成意外对象路径。
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
PREFIX_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class OssUploadError(Exception):
    """Expose only a stable OSS upload category without provider response details."""

    def __init__(self, category: str) -> None:
        """Keep external SDK errors out of logs and DingTalk content."""

        super().__init__(category)
        # category 是唯一允许进入安全运行日志的 OSS 诊断字段。
        self.category = category


@dataclass(frozen=True, slots=True)
class OssSettings:
    """Hold validated OSS credentials and target metadata in process memory only."""

    # enabled 是唯一总开关，缺失凭证不会自动开启网络上传。
    enabled: bool
    region: str | None = None
    endpoint: str | None = None
    bucket: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    object_prefix: str = "compass"
    download_url_expires_seconds: int = MAX_DOWNLOAD_URL_EXPIRES_SECONDS
    error_category: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether enabled settings may create an OSS client."""

        return self.enabled and self.error_category is None


@dataclass(frozen=True, slots=True)
class OssUploadResult:
    """Return only the temporary download URL needed by the notification layer."""

    # download_url 是 bearer URL，只允许在本次进程内流转至钉钉正文。
    download_url: str


def load_oss_settings() -> OssSettings:
    """Read strict OSS settings from the already-loaded process environment."""

    # 显式开关防止开发环境意外访问真实 OSS。
    enabled_text = os.environ.get("OSS_ENABLED", "false").strip().lower()
    if enabled_text not in {"true", "false"}:
        return OssSettings(enabled=False, error_category="oss_config_invalid")
    if enabled_text == "false":
        return OssSettings(enabled=False)
    # 凭证只在此进程内存中读取，后续绝不写入日志或持久化文件。
    region = os.environ.get("OSS_REGION", "").strip()
    endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
    bucket = os.environ.get("OSS_BUCKET", "").strip()
    access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
    object_prefix = os.environ.get("OSS_OBJECT_PREFIX", "compass").strip().strip("/")
    expires_text = os.environ.get(
        "OSS_DOWNLOAD_URL_EXPIRES_SECONDS",
        str(MAX_DOWNLOAD_URL_EXPIRES_SECONDS),
    ).strip()
    try:
        expires_seconds = int(expires_text)
    except ValueError:
        return OssSettings(enabled=True, error_category="oss_config_invalid")
    if (
        not region
        or not endpoint
        or not _is_valid_endpoint(endpoint)
        or BUCKET_PATTERN.fullmatch(bucket) is None
        or not access_key_id
        or not access_key_secret
        or not _is_valid_prefix(object_prefix)
        or not 1 <= expires_seconds <= MAX_DOWNLOAD_URL_EXPIRES_SECONDS
    ):
        return OssSettings(enabled=True, error_category="oss_config_invalid")
    return OssSettings(
        enabled=True,
        region=region,
        endpoint=endpoint,
        bucket=bucket,
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        object_prefix=object_prefix,
        download_url_expires_seconds=expires_seconds,
    )


def _is_valid_endpoint(endpoint: str) -> bool:
    """Accept only an HTTPS OSS endpoint without embedded credentials."""

    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.path.rstrip("/")
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _is_valid_prefix(object_prefix: str) -> bool:
    """Require a non-empty slash-separated OSS object prefix."""

    return bool(object_prefix) and all(
        PREFIX_SEGMENT_PATTERN.fullmatch(segment) is not None
        for segment in object_prefix.split("/")
    )


class OssUploader:
    """Upload a published CSV and return a private time-limited download URL."""

    def __init__(
        self,
        settings: OssSettings,
        *,
        client_factory: Callable[[OssSettings], Any] | None = None,
    ) -> None:
        """Keep the SDK client lazy so disabled OSS never imports or contacts it."""

        self.settings = settings
        # client_factory 让单元测试覆盖调用契约而不请求真实 OSS。
        self._client_factory = client_factory or _build_oss_client

    @classmethod
    def from_environment(cls) -> "OssUploader":
        """Create one uploader from the project dotenv values already loaded by CLI."""

        return cls(load_oss_settings())

    def upload_csv(
        self,
        *,
        csv_path: Path,
        business_date: date,
        task_id: str,
        batch_id: str,
    ) -> OssUploadResult | None:
        """Upload one official CSV and generate its short-lived private download URL."""

        if not self.settings.enabled:
            return None
        if not self.settings.valid:
            raise OssUploadError(self.settings.error_category or "oss_config_invalid")
        if (
            not csv_path.is_file()
            or csv_path.suffix.lower() != ".csv"
            or TASK_ID_PATTERN.fullmatch(task_id) is None
            or not re.fullmatch(r"[0-9a-f]{32}", batch_id)
        ):
            raise OssUploadError("oss_upload_input_invalid")
        # 每个真实发布批次使用独立对象键，避免强制采集覆盖历史 CSV。
        object_key = (
            f"{self.settings.object_prefix}/{business_date.isoformat()}/"
            f"{task_id}/{batch_id}/{csv_path.name}"
        )
        try:
            client, oss = self._client_factory(self.settings)
            upload_result = client.put_object_from_file(
                oss.PutObjectRequest(bucket=self.settings.bucket, key=object_key),
                str(csv_path),
            )
            if not 200 <= int(upload_result.status_code) < 300:
                raise OssUploadError("oss_upload_failed")
            signed_result = client.presign(
                oss.GetObjectRequest(
                    bucket=self.settings.bucket,
                    key=object_key,
                    response_content_disposition=(
                        f'attachment; filename="{csv_path.name}"'
                    ),
                ),
                expires=timedelta(
                    seconds=self.settings.download_url_expires_seconds
                ),
            )
            if not isinstance(signed_result.url, str) or not signed_result.url:
                raise OssUploadError("oss_presign_failed")
            return OssUploadResult(download_url=signed_result.url)
        except OssUploadError:
            raise
        except Exception:
            # SDK 的响应正文、请求 ID 与连接信息均不对外传播。
            raise OssUploadError("oss_upload_failed") from None


def _build_oss_client(settings: OssSettings) -> tuple[Any, Any]:
    """Build an OSS V2 client with in-memory static credentials."""

    try:
        import alibabacloud_oss_v2 as oss
    except ImportError:
        raise OssUploadError("oss_sdk_unavailable") from None
    # 直接创建静态凭证提供器，避免污染进程环境变量并保持命名空间独立。
    credentials_provider = oss.credentials.StaticCredentialsProvider(
        settings.access_key_id,
        settings.access_key_secret,
    )
    configuration = oss.config.load_default()
    configuration.credentials_provider = credentials_provider
    configuration.region = settings.region
    configuration.endpoint = settings.endpoint
    return oss.Client(configuration), oss
