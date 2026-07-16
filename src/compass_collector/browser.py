"""Persistent Chrome lifecycle and runtime authentication extraction."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from compass_collector.config import BrowserConfig


# 固定安全页面不包含追踪、签名或账号参数。
SAFE_RANKING_PAGE_URL = "https://compass.jinritemai.com/shop/chance/rank-product"
# Cookie 查询使用完整 API 路径，让 Playwright 自动应用 domain/path/secure 规则。
COMPASS_API_COOKIE_SCOPE = (
    "https://compass.jinritemai.com/compass_api/shop/product/product_rank/market_hot_sale"
)


@dataclass(slots=True)
class BrowserSession:
    """Own Playwright resources that must be closed in reverse order."""

    playwright: Playwright
    context: BrowserContext
    page: Page

    def close(self) -> None:
        """Close the persistent context and then stop Playwright."""

        self.context.close()
        self.playwright.stop()

    def wait_for_manual_exit(self, message: str) -> None:
        """Keep Chrome visible until the developer explicitly finishes debugging."""

        input(message)

    def user_agent(self) -> str:
        """Read the actual Chrome user agent instead of hard-coding a version."""

        # 页面返回的 User-Agent 不包含 Cookie 或 Token。
        user_agent_value = self.page.evaluate("() => navigator.userAgent")
        if not isinstance(user_agent_value, str) or not user_agent_value:
            raise RuntimeError("Chrome returned an invalid user agent")
        return user_agent_value

    def whitelisted_cookies(self, cookie_names: list[str]) -> list[dict[str, Any]]:
        """Return only applicable Compass cookies whose names are allowlisted."""

        # 名称集合用于快速过滤，不包含任何 Cookie 值。
        allowed_names = set(cookie_names)
        # Playwright 只返回对目标源实际适用的 Cookie。
        applicable_cookies = self.context.cookies([COMPASS_API_COOKIE_SCOPE])
        return [cookie for cookie in applicable_cookies if cookie["name"] in allowed_names]


def open_browser(config: BrowserConfig) -> BrowserSession:
    """Launch the configured persistent Chrome profile and open the safe page."""

    # 持久化 Profile 目录在启动 Chrome 前创建。
    profile_path = Path(config.profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    # Playwright 运行时由 BrowserSession 统一管理生命周期。
    playwright = sync_playwright().start()
    try:
        # 持久化上下文使登录态能跨 CLI 运行保留。
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            channel=config.channel,
            headless=config.headless,
            locale=config.locale,
            timezone_id=config.timezone_id,
        )
    except Exception:
        playwright.stop()
        raise
    # 复用 Chrome 自动创建的首个页面，没有时才新建。
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(SAFE_RANKING_PAGE_URL, wait_until="domcontentloaded")
    return BrowserSession(playwright=playwright, context=context, page=page)
