"""从远程预览或批次固定配置生成安全摘要，不读取凭证或重新计算过滤。"""

from decimal import Decimal

from compass_collector.collection_limits import page_limit


def collection_rule_summary(snapshot):
    """只格式化已知指标；没有远程快照的旧批次明确展示未应用规则。"""
    # 预览和执行都接受同一种批次配置结构，避免界面另写默认规则。
    remote = (snapshot or {}).get("remote_category_config")
    if remote is None:
        return "本地／历史规则 · 页数不限 · 未应用商品过滤" + (
            " · 淘宝→天猫 · 两组共用淘宝页数" if (snapshot or {}).get("filters", {}).get("seller_platforms") else "")
    # 上限来自原批次，不能用今天的服务端配置替换。
    limit = page_limit(snapshot)
    # 仅含版本、限页和已启用条件，不展示完整响应或请求身份。
    parts = [f"远程 v{remote['revision']}", f"每分类最多 {limit} 页" if limit is not None else "页数不限"]
    if (snapshot or {}).get("filters", {}).get("seller_platforms"):
        parts.append("淘宝→天猫 · 两组共用淘宝页数")
    # 两平台固定指标按业务展示顺序列出，未知字段不进入日志或界面。
    metrics = (("pay_buyer_count", "支付买家数", "人"), ("visitor_count", "访客数", "人")) if remote.get("platform", snapshot.get("platform")) == "taobao" else (("pay_amount", "支付金额", "元"), ("pay_combo_count", "成交件数", "件"))
    # 总开关或全部单项关闭时，不把保留的阈值描述为有效规则。
    filters = remote.get("product_filters")
    # conditions 只收集当前启用的已知条件，保持 AND 口径。
    conditions = []
    if filters and filters.get("enabled"):
        # key、name、unit 来自上面的固定指标表，不接受任意字段描述。
        for key, name, unit in metrics:
            # 规则已经由配置服务校验，缺少单项按未启用展示。
            condition = filters.get("conditions", {}).get(key)
            if not condition or not condition.get("enabled"):
                continue
            # 使用十进制格式化，保留金额精度且不引入浮点舍入。
            threshold = format(Decimal(condition["threshold"]), ",f")
            # 只支持配置契约里的两个操作符。
            operator = {"gte": "≥", "gt": ">"}[condition["operator"]]
            conditions.append(f"{name}{operator}{threshold}{unit}")
    parts.append("、".join(conditions) + "（同时满足）" if conditions else ("商品过滤关闭" if filters is not None else "未应用商品过滤"))
    return " · ".join(parts)
