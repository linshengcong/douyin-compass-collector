"""Explicit collector registration; never load module paths from configuration."""


def registered_platforms() -> set[str]:
    """Return implemented production adapters without launching Chrome."""
    return {"compass"}


def create_adapter(platform: str, browser, collection, *, manual, control=None):
    """Build one implemented adapter with caller-owned execution policy."""
    if platform not in registered_platforms():
        raise ValueError("platform adapter is not registered")
    # 延迟导入防止配置校验与 Playwright 初始化相互依赖。
    from compass_collector.platforms.compass import CompassAdapter

    return CompassAdapter(browser, collection, manual=manual, control=control)
