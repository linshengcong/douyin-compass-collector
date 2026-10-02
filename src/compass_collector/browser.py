"""Shared visible Chrome lifecycle; platform navigation belongs to adapters."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from compass_collector.config import BrowserConfig
from compass_collector.errors import BrowserOperationError


def restore_session_cookies(context: BrowserContext, path: Path) -> None:
    """Restore local session cookies before navigation without replacing native cookies."""
    if not path.exists():
        return
    try:
        # 备份仅含当前平台独立 Profile 的会话 Cookie，禁止进入日志或采集材料。
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Unsupported session cookie snapshot")
        # 必须完整验证后再写入浏览器，损坏备份不能静默视为登录成功。
        cookies = payload.get("cookies")
        if not isinstance(cookies, list) or any(
            not isinstance(cookie, dict) or cookie.get("expires") != -1
            for cookie in cookies
        ):
            raise ValueError("Invalid session cookie snapshot")
        # 已被 Chrome 恢复的同名 Cookie 优先，避免旧备份覆盖更新后的原生值。
        existing = {(cookie["name"], cookie["domain"], cookie["path"], cookie.get("partitionKey"))
                    for cookie in context.cookies()}
        missing = [cookie for cookie in cookies
                   if (cookie["name"], cookie["domain"], cookie["path"], cookie.get("partitionKey")) not in existing]
        if missing:
            context.add_cookies(missing)
    except Exception as error:
        raise BrowserOperationError(
            "Local session cookies could not be restored", category="browser_session_state_error",
            failed_step="restore_session_cookies", exception_type=type(error).__name__,
        ) from None


def save_session_cookies(context: BrowserContext, path: Path) -> None:
    """Atomically refresh a private local snapshot, including an empty logged-out state."""
    # 临时文件与目标位于同一目录，替换前 chmod 防止暴露完整凭证。
    temporary_path = None
    try:
        # 持久 Cookie 仍交给 Chrome 自身保存，不修改网站给出的过期时间。
        cookies = [cookie for cookie in context.cookies() if cookie.get("expires") == -1]
        descriptor, temporary_name = tempfile.mkstemp(prefix=".session-cookies-", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary_path, 0o600)
            json.dump({"version": 1, "cookies": cookies}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception as error:
        raise BrowserOperationError(
            "Local session cookies could not be saved", category="browser_session_state_error",
            failed_step="save_session_cookies", exception_type=type(error).__name__,
        ) from None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
    # 关闭时只更新当前 Profile 的本地凭证文件；默认不启用额外备份。
    session_cookie_path: Path | None = None

    def close(self) -> None:
        """Close the persistent context and then stop Playwright."""
        try:
            try:
                if self.session_cookie_path is not None:
                    save_session_cookies(self.context, self.session_cookie_path)
            finally:
                # 备份失败也必须关闭 Chrome 并释放 Profile 锁。
                self.context.close()
        finally:
            self.playwright.stop()

    def wait_for_manual_exit(self, message: str) -> None:
        """Keep the visible page available for explicitly enabled inspection."""
        input(message)


def open_browser(config: BrowserConfig) -> BrowserSession:
    """Open Chrome without navigating before platform listeners are installed."""
    # Profile 目录由平台配置决定，认证备份也只留在这一独立目录。
    profile_path = Path(config.profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    # Playwright 只在当前任务线程中创建、使用和关闭。
    playwright = sync_playwright().start()
    # 启动中失败时仍关闭已创建上下文，避免下一次登录被 Profile 锁阻止。
    context = None
    # 不把凭证放进通用 runtime 数据目录或公开发布目录。
    cookie_path = profile_path / ".session-cookies.json" if config.persist_session_cookies else None
    try:
        # 返回持久化上下文，由平台适配器选择导航入口。
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            channel=config.channel,
            headless=config.headless,
            locale=config.locale,
            timezone_id=config.timezone_id,
        )
        if config.webdriver_compatibility:
            # 按已成功登录的工作树实现覆盖原型 getter，不新增 navigator 实例属性。
            # 首次导航及子 frame 脚本执行前生效；不保证隐藏其他自动化信号。
            context.add_init_script(
                """
                Object.defineProperty(Navigator.prototype, 'webdriver', {
                    get: () => undefined
                });
                """
            )
        if cookie_path is not None:
            restore_session_cookies(context, cookie_path)
    except Exception as error:
        try:
            if context is not None:
                context.close()
        finally:
            playwright.stop()
        if isinstance(error, BrowserOperationError):
            raise
        raise BrowserOperationError(
            "Chrome could not start",
            category="browser_launch_error",
            failed_step="launch_browser",
            exception_type=type(error).__name__,
        ) from error
    # 复用 Chrome 初始标签，不复用用户的日常 Profile。
    page = context.pages[0] if context.pages else context.new_page()
    return BrowserSession(playwright, context, page, cookie_path)
