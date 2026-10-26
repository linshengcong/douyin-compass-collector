"""在淘宝分类规则之后展开店铺分组，保留真实分类参数及历史批次身份。"""

from dataclasses import replace

from compass_collector.errors import ResponseContractError
from compass_collector.models import DiscoveredScope


def split_seller_scopes(discovery, platforms, values):
    """先遍历淘宝全部目标分类，再遍历天猫；两组分别登记独立运行身份。"""
    if not platforms:
        return discovery
    if (platforms != ("taobao", "tmall") or not isinstance(values, dict)
            or set(values) != set(platforms)
            or any(not isinstance(value, str) or not value.isascii() or not value.isdecimal()
                   for value in values.values())
            or len(set(values.values())) != 2):
        raise ResponseContractError("Invalid seller type controls", category="invalid_seller_types")
    # 分组身份参与分类运行索引，但 cate_id、根节点和三级名称仍是平台真实值。
    scopes = []
    for platform in platforms:
        for category in discovery.categories:
            scopes.append(DiscoveredScope(
                discovery_order=len(scopes) + 1,
                key=f"{platform}:{category.key}",
                path=category.path,
                platform_metadata={**category.platform_metadata,
                                   "seller_platform": platform, "seller_type": values[platform]},
            ))
    return replace(discovery, categories=tuple(scopes))
