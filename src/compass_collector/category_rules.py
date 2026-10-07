"""分类规则契约 v1；与采集器同名模块保持逐字一致，验收脚本检查一致性。"""

import json
from hashlib import sha256


def path_key(path):
    """完整祖先 ID 路径是节点身份，不能用名称或末级 ID 替代。"""
    return json.dumps(path, ensure_ascii=False, separators=(",", ":"))


def contains(parent, child):
    """父节点动态覆盖整个子树，包括平台以后新增的三级分类。"""
    return len(parent) <= len(child) and child[:len(parent)] == parent


def validate_paths(paths):
    """接受一至三级字符串 ID 路径，拒绝空白、重复和祖孙冗余规则。"""
    if not isinstance(paths, list):
        raise ValueError("分类路径必须是列表")
    # 已验证路径保持配置输入顺序。
    result = []
    for path in paths:
        if not isinstance(path, list) or not 1 <= len(path) <= 3 or any(
            not isinstance(part, str) or not part or part != part.strip() for part in path
        ):
            raise ValueError("分类 ID 路径无效")
        if any(contains(previous, path) or contains(path, previous) for previous in result):
            raise ValueError("分类规则存在重复或父子冗余")
        result.append(path)
    return result


def validate_catalog(nodes):
    """目录只接受前三层身份和名称，父节点必须存在，不保存请求凭证。"""
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("分类目录不能为空")
    # 索引用于同时校验节点唯一性和父子闭包。
    index = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"path", "name"}:
            raise ValueError("分类目录字段无效")
        validate_paths([node["path"]])
        if not isinstance(node["name"], str) or not node["name"].strip() or node["name"] != node["name"].strip():
            raise ValueError("分类名称无效")
        # 编码完整路径允许不同一级下面出现相同二级 ID。
        key = path_key(node["path"])
        if key in index:
            raise ValueError("分类目录身份重复")
        index[key] = node
    for node in nodes:
        if len(node["path"]) > 1 and path_key(node["path"][:-1]) not in index:
            raise ValueError("分类目录缺少父节点")
    if not any(len(node["path"]) == 3 for node in nodes):
        raise ValueError("分类目录没有三级分类")
    return nodes


def catalog_digest(nodes):
    """顺序也参与摘要，网页预览与实际采集均沿用平台顺序。"""
    return sha256(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_rules(rules):
    """平台规则版本包含任务范围和共享黑名单，不接收其他运行配置。"""
    if not isinstance(rules, dict) or set(rules) != {"tasks", "exclusions", "unresolved_names"}:
        raise ValueError("采集规则字段无效")
    if not isinstance(rules["tasks"], list) or not rules["tasks"]:
        raise ValueError("至少需要一个采集任务")
    # 任务身份不能因多个任务同名而覆盖。
    ids = set()
    for task in rules["tasks"]:
        if not isinstance(task, dict) or set(task) != {"id", "display_name", "targets"}:
            raise ValueError("采集任务字段无效")
        if not isinstance(task["id"], str) or not task["id"] or task["id"] in ids:
            raise ValueError("采集任务身份重复或无效")
        if not isinstance(task["display_name"], str) or not task["display_name"].strip():
            raise ValueError("采集任务名称无效")
        ids.add(task["id"])
        validate_paths(task["targets"])
    validate_paths(rules["exclusions"])
    if any(len(path) == 1 for path in rules["exclusions"]):
        raise ValueError("黑名单只支持二级、三级分类")
    if not isinstance(rules["unresolved_names"], list) or any(
        not isinstance(name, str) or not name.strip() for name in rules["unresolved_names"]
    ):
        raise ValueError("未匹配名称格式无效")
    return rules


def preview_rules(nodes, rules, task_id):
    """展开三级目标后应用排除；失效范围和活动黑名单阻断，范围外仅提示。"""
    validate_catalog(nodes)
    validate_rules(rules)
    # 任务由调用方明确选择，不能降级为第一个任务。
    task = next((task for task in rules["tasks"] if task["id"] == task_id), None)
    if task is None:
        raise ValueError("找不到采集任务")
    # 索引用于缺失检测及名称路径展示。
    index = {path_key(node["path"]): node for node in nodes}
    # 输出完整计划，供前端展示和采集器一致性验收。
    included, excluded, issues, inactive = [], [], [], []
    for target in task["targets"]:
        if path_key(target) not in index:
            issues.append({"path": target, "message": "已选采集分类不存在"})
        elif not any(len(node["path"]) == 3 and contains(target, node["path"]) for node in nodes):
            issues.append({"path": target, "message": "已选范围没有三级分类"})
    for blocked in rules["exclusions"]:
        # 祖孙相交即表示本任务黑名单仍具有业务影响。
        active = any(contains(target, blocked) or contains(blocked, target) for target in task["targets"])
        if path_key(blocked) not in index:
            (issues if active else inactive).append({"path": blocked, "message": "黑名单分类不存在"})
    # 按明确目标顺序执行，单一父范围内部仍保持平台源顺序。
    ordered_nodes = [node for target in task["targets"] for node in nodes
                     if len(node["path"]) == 3 and contains(target, node["path"])]
    for node in ordered_nodes:
        # 名称路径只用于解释规则，不参与规则命中。
        names = [index[path_key(node["path"][:level])]["name"] for level in (1, 2, 3)]
        # 最先匹配的排除节点用于确定解释来源。
        reason = next((path for path in rules["exclusions"] if contains(path, node["path"])), None)
        # 每条预览记录明确保留身份和完整展示路径。
        row = {"path": node["path"], "names": names, "reason": reason}
        (excluded if reason else included).append(row)
    if rules["unresolved_names"]:
        issues.append({"path": [], "message": "旧名称黑名单尚未处理"})
    if not included:
        issues.append({"path": [], "message": "没有有效的三级采集分类"})
    return {"included": included, "excluded": excluded, "issues": issues,
            "inactive": inactive, "valid": not issues}
