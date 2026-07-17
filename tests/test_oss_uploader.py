"""Private OSS CSV upload and short-lived signed-link tests."""

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from compass_collector.oss_uploader import (
    MAX_DOWNLOAD_URL_EXPIRES_SECONDS,
    OssSettings,
    OssUploadError,
    OssUploader,
    load_oss_settings,
)


class _FakeClient:
    """Capture SDK-shaped calls without accessing real OSS."""

    def __init__(self) -> None:
        """Initialize deterministic in-memory call captures."""

        self.upload_calls: list[tuple[object, str]] = []
        self.presign_calls: list[tuple[object, timedelta]] = []

    def put_object_from_file(self, request: object, path: str) -> object:
        """Return a successful simple-upload result."""

        self.upload_calls.append((request, path))
        return SimpleNamespace(status_code=200)

    def presign(self, request: object, *, expires: timedelta) -> object:
        """Return a fake signed URL that must only flow to DingTalk."""

        self.presign_calls.append((request, expires))
        return SimpleNamespace(url="https://example.invalid/private.csv?signature=fake")


class _FakeOss:
    """Provide request constructors matching the OSS SDK boundary."""

    class PutObjectRequest:
        """Store upload request fields for assertions."""

        def __init__(self, **kwargs: object) -> None:
            """Keep request values visible to the test double only."""

            self.__dict__.update(kwargs)

    class GetObjectRequest:
        """Store signed-download request fields for assertions."""

        def __init__(self, **kwargs: object) -> None:
            """Keep request values visible to the test double only."""

            self.__dict__.update(kwargs)


@pytest.fixture(autouse=True)
def clear_oss_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent developer OSS credentials from affecting unit tests."""

    for variable_name in (
        "OSS_ENABLED",
        "OSS_REGION",
        "OSS_ENDPOINT",
        "OSS_BUCKET",
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "OSS_OBJECT_PREFIX",
        "OSS_DOWNLOAD_URL_EXPIRES_SECONDS",
    ):
        monkeypatch.delenv(variable_name, raising=False)


def _settings() -> OssSettings:
    """Build a safe in-memory enabled configuration."""

    return OssSettings(
        enabled=True,
        region="cn-hangzhou",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="compass-export-test",
        access_key_id="fake-id",
        access_key_secret="fake-secret",
    )


def test_disabled_oss_skips_upload_without_creating_an_sdk_client(tmp_path: Path) -> None:
    """Keep the default disabled configuration outside the collection data path."""

    csv_path = tmp_path / "榜单.csv"
    csv_path.write_text("分类\n食品\n", encoding="utf-8")
    uploader = OssUploader(
        OssSettings(enabled=False),
        client_factory=lambda _: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert uploader.upload_csv(
        csv_path=csv_path,
        business_date=date(2026, 7, 17),
        task_id="product_hot_sale_all_level3",
        batch_id="a" * 32,
    ) is None


def test_enabled_oss_uploads_private_csv_and_generates_seven_day_url(tmp_path: Path) -> None:
    """Use a batch-unique key and the configured seven-day expiry boundary."""

    csv_path = tmp_path / "全行业三级分类商品实时榜_1200.csv"
    csv_path.write_text("分类\n食品\n", encoding="utf-8")
    client = _FakeClient()
    uploader = OssUploader(_settings(), client_factory=lambda _: (client, _FakeOss))

    result = uploader.upload_csv(
        csv_path=csv_path,
        business_date=date(2026, 7, 17),
        task_id="product_hot_sale_all_level3",
        batch_id="a" * 32,
    )

    assert result is not None
    assert result.download_url.endswith("signature=fake")
    upload_request, uploaded_path = client.upload_calls[0]
    assert upload_request.key == (
        "compass/2026-07-17/product_hot_sale_all_level3/"
        f"{'a' * 32}/{csv_path.name}"
    )
    assert uploaded_path == str(csv_path)
    _, expires = client.presign_calls[0]
    assert expires == timedelta(seconds=MAX_DOWNLOAD_URL_EXPIRES_SECONDS)


def test_invalid_enabled_configuration_is_reported_without_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require a complete private OSS configuration before external access."""

    monkeypatch.setenv("OSS_ENABLED", "true")
    monkeypatch.setenv("OSS_REGION", "cn-hangzhou")

    settings = load_oss_settings()

    assert settings.valid is False
    assert settings.error_category == "oss_config_invalid"


def test_expiry_cannot_exceed_official_seven_day_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an invalid long-lived bearer URL before any SDK call."""

    monkeypatch.setenv("OSS_ENABLED", "true")
    monkeypatch.setenv("OSS_REGION", "cn-hangzhou")
    monkeypatch.setenv("OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
    monkeypatch.setenv("OSS_BUCKET", "compass-export-test")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "fake-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "fake-secret")
    monkeypatch.setenv(
        "OSS_DOWNLOAD_URL_EXPIRES_SECONDS",
        str(MAX_DOWNLOAD_URL_EXPIRES_SECONDS + 1),
    )

    assert load_oss_settings().error_category == "oss_config_invalid"


def test_upload_failure_keeps_provider_details_out_of_the_error(tmp_path: Path) -> None:
    """Expose only a stable category when the SDK request fails."""

    csv_path = tmp_path / "榜单.csv"
    csv_path.write_text("分类\n食品\n", encoding="utf-8")

    def failing_factory(_: OssSettings) -> tuple[object, object]:
        """Raise a provider-shaped exception containing sensitive-looking text."""

        raise RuntimeError("https://secret.invalid/?token=must-not-leak")

    uploader = OssUploader(_settings(), client_factory=failing_factory)
    with pytest.raises(OssUploadError, match="oss_upload_failed") as error_info:
        uploader.upload_csv(
            csv_path=csv_path,
            business_date=date(2026, 7, 17),
            task_id="product_hot_sale_all_level3",
            batch_id="a" * 32,
        )

    assert "secret.invalid" not in str(error_info.value)
