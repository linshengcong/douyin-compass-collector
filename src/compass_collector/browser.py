"""Shared visible Chrome lifecycle; platform navigation belongs to adapters."""

from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from compass_collector.config import BrowserConfig
from compass_collector.errors import BrowserOperationError


def build_page_error(
    page: Page, error: Exception, *, failed_step: str
) -> BrowserOperationError:
    """Capture local diagnostics without request URLs or exception text."""
    try:
        # 截图只返回给本地受限失败材料。
        screenshot = page.screenshot(type="png", timeout=3000)
    except Exception:
        screenshot = None
    return BrowserOperationError(
        "Browser page operation failed",
        category="browser_page_error",
        failed_step=failed_step,
        exception_type=type(error).__name__,
        screenshot=screenshot,
    )


@dataclass(slots=True)
class BrowserSession:
    """Own Playwright resources in their creating thread."""

    playwright: Playwright
    context: BrowserContext
    page: Page

    def close(self) -> None:
        """Close the persistent context and then stop Playwright."""
        try:
            self.context.close()
        finally:
            self.playwright.stop()

    def wait_for_manual_exit(self, message: str) -> None:
        """Keep the visible page available for explicitly enabled inspection."""
        input(message)


def open_browser(config: BrowserConfig) -> BrowserSession:
    """Open Chrome without navigating before platform listeners are installed."""
    # Profile 目录由平台配置决定，不导出认证信息。
    profile_path = Path(config.profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    # Playwright 只在当前任务线程中创建、使用和关闭。
    playwright = sync_playwright().start()
    try:
        # 返回持久化上下文，由平台适配器选择导航入口。
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            channel=config.channel,
            headless=config.headless,
            locale=config.locale,
            timezone_id=config.timezone_id,
        )
    except Exception as error:
        playwright.stop()
        raise BrowserOperationError(
            "Chrome could not start",
            category="browser_launch_error",
            failed_step="launch_browser",
            exception_type=type(error).__name__,
        ) from error
    # 复用 Chrome 初始标签，不复用用户的日常 Profile。
    page = context.pages[0] if context.pages else context.new_page()
    return BrowserSession(playwright, context, page)
