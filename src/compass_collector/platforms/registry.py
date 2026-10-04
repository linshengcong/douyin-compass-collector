"""Explicit collector registration; never load module paths from configuration."""


def registered_platforms() -> set[str]:
    """Return implemented production adapters without launching Chrome."""
    return {"compass", "taobao"}


def create_adapter(platform: str, browser, collection, *, manual, control=None):
    """Build one implemented adapter with caller-owned execution policy."""
    if platform not in registered_platforms():
        raise ValueError("platform adapter is not registered")
    if platform == "taobao":
        # 淘宝使用独立页面控制器，共享入口不承担平台 DOM 或认证逻辑。
        from compass_collector.platforms.taobao import TaobaoAdapter
        from compass_collector.platforms.taobao_controls import TaobaoBrowserControls

        return TaobaoAdapter(browser, collection, manual=manual, control=control,
                             controls=TaobaoBrowserControls(collection, control))
    # 延迟导入防止配置校验与 Playwright 初始化相互依赖。
    from compass_collector.platforms.compass import CompassAdapter

    return CompassAdapter(browser, collection, manual=manual, control=control)
