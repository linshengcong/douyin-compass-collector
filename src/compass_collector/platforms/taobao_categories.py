"""Rebuild the observed flat taxonomy without interpreting unverified columns."""

from collections import defaultdict

from compass_collector.errors import ResponseContractError
from compass_collector.models import CategoryDiscoveryResult, DiscoveredScope

# 前四列由真实响应及官方分类选择器共同确认。
CATEGORY_TREE_ENDPOINT_PATH = "/mc/common/free/getCateInfo.json"


def category_id(value, *, allow_zero: bool = False) -> str:
    """Normalize numeric source IDs while rejecting booleans and malformed IDs."""
    if type(value) not in (int, str) or not str(value).isascii() or not str(value).isdecimal():
        raise ResponseContractError("Invalid Taobao category ID", category="invalid_category_tree")
    # 规范化字符串让整数与字符串来源使用同一父子索引。
    normalized = str(int(value))
    if normalized == "0" and not allow_zero:
        raise ResponseContractError("Invalid Taobao category ID", category="invalid_category_tree")
    return normalized


def parse_category_tree(payload: dict, selection) -> CategoryDiscoveryResult:
    """Resolve the entire configured scope before ranking collection."""
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        raise ResponseContractError("Taobao category response failed", category="taobao_business_error")
    if not isinstance(payload.get("data"), list):
        raise ResponseContractError("Taobao categories are not an array", category="invalid_category_tree")
    # 原始顺序保存在子节点列表；相同 ID 的行保留到目标分支检查。
    children = defaultdict(list)
    by_id = defaultdict(list)
    for row in payload["data"]:
        if not isinstance(row, list) or len(row) < 4:
            raise ResponseContractError("Invalid Taobao category row", category="invalid_category_tree")
        # 这里只解释已确认的四列，其余字段不参与深度判断。
        parent = category_id(row[0], allow_zero=True)
        identity = category_id(row[1])
        name, flag = row[2], row[3]
        if not isinstance(name, str) or not name.strip() or type(flag) is not int or flag < 0:
            raise ResponseContractError("Invalid Taobao category metadata", category="invalid_category_tree")
        # 每个节点保存身份、名称、请求标志及父节点。
        node = (identity, name.strip(), flag, parent)
        children[parent].append(node)
        by_id[identity].append(node)
    # 主根保留全量语义，追加根只展开明确指定的二级分支。
    configured_roots = [(selection.root_category_id, ())] + [
        (root.root_category_id, tuple(root.level2_category_ids))
        for root in selection.additional_roots
    ]
    # 所有配置根必须是唯一的一级节点；先验证再构造任何采集计划。
    roots = []
    for root_id, level2_ids in configured_roots:
        # 不能将二级或重复身份误当作一级根节点。
        candidates = by_id.get(root_id, [])
        if len(candidates) != 1 or candidates[0][3] != "0":
            raise ResponseContractError("Configured Taobao root is invalid", category="invalid_configured_category")
        roots.append((candidates[0], level2_ids))
    # visited 拒绝目标前三层的重复身份；更深节点不会被访问。
    visited = set()
    scopes = []

    def visit(node, path, identities) -> None:
        """Walk exactly three source levels, rejecting cycles and ambiguity."""
        # 路径与 ID 路径分别用于展示及实际请求元数据。
        identity, name, flag, parent = node
        if name == "全部":
            return
        if identity in visited or len(by_id[identity]) != 1:
            raise ResponseContractError("Ambiguous Taobao category branch", category="invalid_category_tree")
        visited.add(identity)
        # 完整路径长度是层级，cateFlag 不参与层级计算。
        next_path = (*path, name)
        next_ids = (*identities, identity)
        if len(next_path) == 3:
            scopes.append(DiscoveredScope(
                len(scopes) + 1, identity, next_path,
                {"root_category_id": next_ids[0], "parent_cate_id": parent,
                 "cate_id": identity, "cate_flag": str(flag)},
            ))
            return
        for child in children.get(identity, []):
            visit(child, next_path, next_ids)

    for root, level2_ids in roots:
        # 每个配置根都必须贡献三级分类，不能静默忽略无效追加范围。
        root_start = len(scopes)
        if not level2_ids:
            visit(root, (), ())
        else:
            visited.add(root[0])
            for level2_id in level2_ids:
                # 二级分类必须唯一且直属于当前配置根，不接受跨根或三级 ID。
                branches = by_id.get(level2_id, [])
                if len(branches) != 1 or branches[0][3] != root[0]:
                    raise ResponseContractError("Configured Taobao branch is invalid", category="invalid_configured_category")
                # 任一指定分支为空时整批报错，避免将遗漏误报为采集完成。
                branch_start = len(scopes)
                visit(branches[0], (root[1],), (root[0],))
                if len(scopes) == branch_start:
                    raise ResponseContractError("Taobao branch has no third-level categories", category="invalid_configured_category")
        if len(scopes) == root_start:
            raise ResponseContractError("Taobao root has no third-level categories", category="invalid_configured_category")
    if selection.mode == "selected":
        # 指定列表全部先验证，再按用户配置顺序重新编号。
        candidates = {scope.key: scope for scope in scopes}
        if any(identity not in candidates for identity in selection.targets):
            raise ResponseContractError("Invalid configured Taobao category", category="invalid_configured_category")
        scopes = [
            DiscoveredScope(index, candidates[identity].key, candidates[identity].path,
                            candidates[identity].platform_metadata)
            for index, identity in enumerate(selection.targets, 1)
        ]
    # 多根批次没有单一根身份，每个分类仍保存自己的完整路径和根 ID。
    return CategoryDiscoveryResult(
        roots[0][0][0] if len(roots) == 1 else None,
        roots[0][0][1] if len(roots) == 1 else None,
        tuple(scopes),
    )
