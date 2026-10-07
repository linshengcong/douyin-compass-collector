"""平台目录规范化及三级请求元数据；网页仅接收节点身份与名称。"""

from compass_collector.category_rules import validate_catalog
from compass_collector.models import CategoryDiscoveryResult, DiscoveredScope
from compass_collector.errors import ResponseContractError


def catalog_from_payload(platform, payload):
    """解析完整前三层目录，不应用任务范围或黑名单。"""
    # 节点与三级请求元数据分别保存，只有节点可上传服务端。
    nodes, scopes = [], []
    if platform == "compass":
        # 复用已验证抖音契约，保持聚合节点排除与源顺序。
        from compass_collector.platforms.compass_categories import parse_category_tree
        discovery = parse_category_tree(payload)
        def visit_compass(node, path):
            """目录保留暂时没有三级子项的父节点，选中时由预览报告空范围。"""
            # 来源节点已通过现有解析器校验，聚合项仍必须排除。
            identity, name = node["cate_id"].strip(), node["cate_name"].strip()
            if identity == "0" or name == "全部":
                return
            # 每层路径保留真实 ID，不根据名称重建目录。
            next_path = [*path, identity]
            nodes.append({"path": next_path, "name": name})
            if len(next_path) < 3:
                for child in node["children"]:
                    visit_compass(child, next_path)

        for node in payload["data"]["cate_list"]:
            visit_compass(node, [])
        scopes.extend(discovery.categories)
    elif platform == "taobao":
        from compass_collector.platforms.taobao_categories import category_id
        if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0 or not isinstance(payload.get("data"), list):
            raise ResponseContractError("淘宝分类响应无效", category="invalid_category_tree")
        # 平面目录先建立唯一身份索引，再按源顺序展开所有一级根。
        children, identities = {}, set()
        for row in payload["data"]:
            if not isinstance(row, list) or len(row) < 4 or not isinstance(row[2], str) or not row[2].strip() or type(row[3]) is not int or row[3] < 0:
                raise ResponseContractError("淘宝分类节点无效", category="invalid_category_tree")
            # 前四列仍是唯一被解释的平台字段。
            parent, identity, name, flag = category_id(row[0], allow_zero=True), category_id(row[1]), row[2].strip(), row[3]
            if identity in identities:
                raise ResponseContractError("淘宝分类身份重复", category="invalid_category_tree")
            identities.add(identity)
            children.setdefault(parent, []).append((identity, name, flag))

        def visit(node, path, names):
            """只展开前三层，四级不参与规则或采集。"""
            # 聚合节点不进入目录，与现有解析器保持一致。
            identity, name, flag = node
            if name == "全部":
                return
            # 新路径只根据父子关系生成，不解释 flag 的层级含义。
            next_path, next_names = [*path, identity], (*names, name)
            nodes.append({"path": next_path, "name": name})
            if len(next_path) == 3:
                scopes.append(DiscoveredScope(len(scopes) + 1, identity, next_names,
                    {"root_category_id": next_path[0], "parent_cate_id": next_path[1],
                     "cate_id": identity, "cate_flag": str(flag)}))
                return
            for child in children.get(identity, []):
                visit(child, next_path, next_names)

        for root in children.get("0", []):
            visit(root, [], ())
    else:
        raise ValueError("不支持的电商平台")
    validate_catalog(nodes)
    return nodes, CategoryDiscoveryResult(None, None, tuple(scopes))


def scope_path(scope):
    """请求元数据投影成完整 ID 路径，与网页规则身份一致。"""
    return [scope.level1_category_id, scope.level2_category_id, scope.category_id]
