"""Trigger and verify one Vercel Deploy Hook without logging its secret URL."""

import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


# Vercel 部署轮询固定为用户确认的五分钟上限与十秒周期。
VERCEL_POLL_INTERVAL_SECONDS = 10
VERCEL_POLL_TIMEOUT_SECONDS = 5 * 60


class VercelDeploymentError(Exception):
    """Expose only a stable deployment error category."""

    def __init__(self, category: str) -> None:
        """Prevent hook URLs and API details from escaping the integration boundary."""

        super().__init__(category)
        # category 是唯一进入日志和钉钉的外部部署诊断。
        self.category = category


@dataclass(frozen=True, slots=True)
class VercelSettings:
    """Hold the deploy-hook and read-only API credentials in process memory."""

    # enabled 是独立总开关，避免 WEB_ENABLED 误触发 Vercel。
    enabled: bool
    # deploy_hook_url 是可触发部署的机密 URL，永不写日志。
    deploy_hook_url: str | None = None
    # api_token 只用于查询本次生产部署状态。
    api_token: str | None = None
    # project_id 精确限定轮询范围，防止读取其它项目的部署。
    project_id: str | None = None
    # site_url 是第二条钉钉消息可安全展示的公开页面地址。
    site_url: str | None = None
    # team_id 仅在项目归属团队时追加给 Vercel API。
    team_id: str | None = None
    # error_category 用于区分未配置和网络/构建失败。
    error_category: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether a deployment can be triggered and observed safely."""

        return (
            self.enabled
            and self.deploy_hook_url is not None
            and self.api_token is not None
            and self.project_id is not None
            and self.site_url is not None
            and self.error_category is None
        )


@dataclass(frozen=True, slots=True)
class VercelDeploymentResult:
    """Return only the public production URL after Vercel reports READY."""

    # site_url 是固定公开入口，不返回临时 preview URL 或 Vercel 内部标识。
    site_url: str


def load_vercel_settings() -> VercelSettings:
    """Load strict optional Vercel settings from the already loaded dotenv file."""

    enabled_text = os.environ.get("VERCEL_ENABLED", "false").strip().lower()
    if enabled_text not in {"true", "false"}:
        return VercelSettings(enabled=False, error_category="vercel_config_invalid")
    if enabled_text == "false":
        return VercelSettings(enabled=False)
    deploy_hook_url = os.environ.get("VERCEL_DEPLOY_HOOK_URL", "").strip()
    api_token = os.environ.get("VERCEL_API_TOKEN", "").strip()
    project_id = os.environ.get("VERCEL_PROJECT_ID", "").strip()
    site_url = os.environ.get("VERCEL_SITE_URL", "").strip().rstrip("/")
    team_id = os.environ.get("VERCEL_TEAM_ID", "").strip() or None
    if (
        not _is_valid_https_url(deploy_hook_url)
        or not api_token
        or not project_id
        or not _is_valid_https_url(site_url)
    ):
        return VercelSettings(enabled=True, error_category="vercel_config_invalid")
    return VercelSettings(
        enabled=True,
        deploy_hook_url=deploy_hook_url,
        api_token=api_token,
        project_id=project_id,
        site_url=site_url,
        team_id=team_id,
    )


def _is_valid_https_url(value: str) -> bool:
    """Accept only a plain HTTPS URL without embedded credentials."""

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
        and not parsed.fragment
    )


class VercelDeployer:
    """Trigger a connected-Git Deploy Hook and poll its resulting production deployment."""

    def __init__(
        self,
        settings: VercelSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
        clock: Any = time.time,
    ) -> None:
        """Inject network and time seams without persisting Vercel credentials."""

        self.settings = settings
        self.transport = transport
        # sleep 和 clock 支持无等待的确定性集成测试。
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def from_environment(cls) -> "VercelDeployer":
        """Create the optional deployer after dotenv has populated this process."""

        return cls(load_vercel_settings())

    def deploy_and_wait(self) -> VercelDeploymentResult | None:
        """Trigger one deployment and wait until it reaches READY, ERROR, or timeout."""

        if not self.settings.enabled:
            return None
        if not self.settings.valid:
            raise VercelDeploymentError(
                self.settings.error_category or "vercel_config_invalid"
            )
        assert self.settings.deploy_hook_url is not None
        assert self.settings.api_token is not None
        assert self.settings.project_id is not None
        assert self.settings.site_url is not None
        started_at_ms = int(self._clock() * 1000)
        timeout = httpx.Timeout(connect=10, read=20, write=20, pool=10)
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                trigger_response = client.post(self.settings.deploy_hook_url)
                if not 200 <= trigger_response.status_code < 300:
                    raise VercelDeploymentError("vercel_deploy_trigger_failed")
                try:
                    trigger_payload = trigger_response.json()
                except ValueError as error:
                    raise VercelDeploymentError("vercel_deploy_trigger_failed") from error
                if not isinstance(trigger_payload, dict) or not isinstance(
                    trigger_payload.get("job"), dict
                ):
                    raise VercelDeploymentError("vercel_deploy_trigger_failed")
                deadline = self._clock() + VERCEL_POLL_TIMEOUT_SECONDS
                deployment_id: str | None = None
                while self._clock() <= deadline:
                    if deployment_id is None:
                        deployment_id = self._find_new_production_deployment(client, started_at_ms)
                    if deployment_id is not None:
                        state = self._deployment_state(client, deployment_id)
                        if state == "READY":
                            return VercelDeploymentResult(site_url=self.settings.site_url)
                        if state in {"ERROR", "CANCELED"}:
                            raise VercelDeploymentError("vercel_deploy_failed")
                    self._sleep(VERCEL_POLL_INTERVAL_SECONDS)
        except VercelDeploymentError:
            raise
        except httpx.TimeoutException:
            raise VercelDeploymentError("vercel_deploy_timeout") from None
        except httpx.RequestError:
            raise VercelDeploymentError("vercel_deploy_network_error") from None
        raise VercelDeploymentError("vercel_deploy_timeout")

    def _api_params(self) -> dict[str, str | int]:
        """Build the project-scoped query parameters for every Vercel API read."""

        assert self.settings.project_id is not None
        params: dict[str, str | int] = {
            "projectId": self.settings.project_id,
            "target": "production",
            "limit": 20,
        }
        if self.settings.team_id is not None:
            params["teamId"] = self.settings.team_id
        return params

    def _find_new_production_deployment(
        self,
        client: httpx.Client,
        started_at_ms: int,
    ) -> str | None:
        """Locate the newest production deployment created after the hook trigger."""

        assert self.settings.api_token is not None
        response = client.get(
            "https://api.vercel.com/v6/deployments",
            headers={"Authorization": f"Bearer {self.settings.api_token}"},
            params=self._api_params(),
        )
        if not 200 <= response.status_code < 300:
            raise VercelDeploymentError("vercel_deploy_status_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise VercelDeploymentError("vercel_deploy_status_failed") from error
        deployments = payload.get("deployments") if isinstance(payload, dict) else None
        if not isinstance(deployments, list):
            raise VercelDeploymentError("vercel_deploy_status_failed")
        candidates = [
            deployment
            for deployment in deployments
            if isinstance(deployment, dict)
            and isinstance(deployment.get("uid"), str)
            and isinstance(deployment.get("createdAt"), int)
            and deployment["createdAt"] >= started_at_ms
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda deployment: int(deployment["createdAt"]))
        return str(latest["uid"])

    def _deployment_state(self, client: httpx.Client, deployment_id: str) -> str:
        """Read one deployment state using the project-scoped Vercel REST endpoint."""

        assert self.settings.api_token is not None
        response = client.get(
            f"https://api.vercel.com/v13/deployments/{deployment_id}",
            headers={"Authorization": f"Bearer {self.settings.api_token}"},
            params={
                key: value
                for key, value in self._api_params().items()
                if key != "limit"
            },
        )
        if not 200 <= response.status_code < 300:
            raise VercelDeploymentError("vercel_deploy_status_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise VercelDeploymentError("vercel_deploy_status_failed") from error
        ready_state = payload.get("readyState") if isinstance(payload, dict) else None
        if not isinstance(ready_state, str):
            raise VercelDeploymentError("vercel_deploy_status_failed")
        return ready_state
