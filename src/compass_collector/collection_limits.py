"""批次固定的分页计划；平台总数与实际计划条数分开保存。"""


def page_limit(snapshot):
    """只读取原批次远程规则；缺省代表旧批次完整采集。"""
    # 本地配置不能覆盖已发布的平台规则，历史无规则继续不限页。
    limits = (snapshot or {}).get("remote_category_config", {}).get("collection_limits")
    if limits is None:
        return None
    if not isinstance(limits, dict) or set(limits) != {"max_pages_per_category"}:
        raise ValueError("invalid collection limits")
    # bool 不能作为页数，零、负数和小数均拒绝。
    value = limits["max_pages_per_category"]
    if type(value) is not int or value < 1:
        raise ValueError("invalid maximum pages")
    return value


def pagination_counts(total, page_size, limit=None):
    """空榜仍采第一页；尾页按平台总数验证，不用过滤数量补页。"""
    # 完整总数保留供平台响应一致性检查。
    pages = max(1, (total + page_size - 1) // page_size)
    if limit is not None:
        pages = min(pages, limit)
    return pages, min(total, pages * page_size)


def frozen_task(task, snapshot):
    """运行时注入已固定页数；不会更改原 YAML 或批次审计。"""
    # 运行配置只对这次分类迭代有效，续采也从原快照恢复。
    return task.model_copy(update={"max_pages_per_category": page_limit(snapshot)})
