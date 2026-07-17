"""Persistent Chrome lifecycle and runtime authentication extraction."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from compass_collector.config import BrowserConfig
from compass_collector.errors import BrowserOperationError


# 固定安全页面不包含追踪、签名或账号参数。
SAFE_RANKING_PAGE_URL = "https://compass.jinritemai.com/shop/chance/rank-product"
# Cookie 查询使用完整 API 路径，让 Playwright 自动应用 domain/path/secure 规则。
COMPASS_API_COOKIE_SCOPES = (
    "https://compass.jinritemai.com/compass_api/config_center/category/cate_list",
    "https://compass.jinritemai.com/compass_api/shop/product/product_rank/market_hot_sale",
)
# 诊断材料只记录固定安全入口的路径，不保存完整 URL。
SAFE_RANKING_PAGE_PATH = "/shop/chance/rank-product"


def build_page_error(
    page: Page,
    error: Exception,
    *,
    failed_step: str,
) -> BrowserOperationError:
    """Capture best-effort safe page metadata without exception text."""

    try:
        # 截图以字节形式返回，由 BatchStorage 原子落盘。
        screenshot = page.screenshot(type="png")
    except Exception:
        screenshot = None
    try:
        # 页面标题只作为可见诊断摘要并在错误类型中限长。
        page_title = page.title()
    except Exception:
        page_title = None
    return BrowserOperationError(
        "Chrome page operation failed",
        category="browser_page_error",
        failed_step=failed_step,
        exception_type=type(error).__name__,
        safe_page_path=SAFE_RANKING_PAGE_PATH,
        page_title=page_title,
        screenshot=screenshot,
    )


@dataclass(slots=True)
class BrowserSession:
    """Own Playwright resources that must be closed in reverse order."""

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
        """Keep Chrome visible until the developer explicitly finishes debugging."""

        input(message)

    def user_agent(self) -> str:
        """Read the actual Chrome user agent instead of hard-coding a version."""

        # 页面返回的 User-Agent 不包含 Cookie 或 Token。
        try:
            # 页面表达式固定且不读取 Cookie 或本地存储。
            user_agent_value = self.page.evaluate("() => navigator.userAgent")
        except Exception as error:
            raise build_page_error(
                self.page, error, failed_step="read_user_agent"
            ) from error
        if not isinstance(user_agent_value, str) or not user_agent_value:
            # validation_error 只提供稳定异常类型，错误文本不会进入诊断材料。
            validation_error = ValueError("invalid browser user agent")
            raise build_page_error(
                self.page,
                validation_error,
                failed_step="read_user_agent",
            ) from validation_error
        return user_agent_value

    def whitelisted_cookies(self, cookie_names: list[str]) -> list[dict[str, Any]]:
        """Return only applicable Compass cookies whose names are allowlisted."""

        # 名称集合用于快速过滤，不包含任何 Cookie 值。
        allowed_names = set(cookie_names)
        # Playwright 只返回对目标源实际适用的 Cookie。
        try:
            # Cookie 值只返回给内存中的 httpx 客户端。
            applicable_cookies = self.context.cookies(list(COMPASS_API_COOKIE_SCOPES))
        except Exception as error:
            raise build_page_error(
                self.page, error, failed_step="read_authentication"
            ) from error
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
    except Exception as error:
        playwright.stop()
        raise BrowserOperationError(
            "Chrome could not start",
            category="browser_launch_error",
            failed_step="launch_browser",
            exception_type=type(error).__name__,
        ) from error
    # 复用 Chrome 自动创建的首个页面，没有时才新建。
    page = context.pages[0] if context.pages else context.new_page()
    try:
        # 页面导航只访问已确认的固定安全入口。
        page.goto(SAFE_RANKING_PAGE_URL, wait_until="domcontentloaded")
    except Exception as error:
        # 导航失败时尽量先捕获页面，再释放浏览器资源。
        page_error = build_page_error(page, error, failed_step="open_safe_page")
        try:
            context.close()
        except Exception:
            # 诊断阶段的关闭错误不能覆盖原始安全页面错误。
            pass
        playwright.stop()
        raise page_error from error
    return BrowserSession(playwright=playwright, context=context, page=page)
