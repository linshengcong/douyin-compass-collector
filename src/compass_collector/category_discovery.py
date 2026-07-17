"""Dynamic category-tree request contract and level-three discovery parser."""

from typing import Any

from compass_collector.errors import (
    CategoryDiscoveryEmptyError,
    ResponseContractError,
)
from compass_collector.models import CategoryDiscoveryResult, DiscoveredCategory


# 分类接口路径不包含追踪、签名或账号参数。
CATEGORY_TREE_ENDPOINT_PATH = "/compass_api/config_center/category/cate_list"
# 首版只保留已确认的三个分类业务参数。
CATEGORY_TREE_REQUEST_PARAMS = {
    "level": 4,
    "scene": 9,
    "default_cate_to_level": 2,
}


def build_category_request_params() -> dict[str, int]:
    """Return a fresh minimal parameter mapping for one category-tree request."""

    # 返回副本避免调用方修改模块级契约。
    return dict(CATEGORY_TREE_REQUEST_PARAMS)


def _node_identity(node: Any, *, context: str) -> tuple[str, str]:
    """Validate and return one consumed category node's ID and display name."""

    if not isinstance(node, dict):
        raise ResponseContractError(
            f"{context} category node is not an object",
            category="invalid_category_tree",
        )
    # 分类 ID 保留平台原始字符串语义。
    category_id = node.get("cate_id")
    # 分类名称用于根定位、路径展示和“全部”排除。
    category_name = node.get("cate_name")
    if not isinstance(category_id, str) or not category_id.strip():
        raise ResponseContractError(
            f"{context} category id is invalid",
            category="invalid_category_tree",
        )
    if not isinstance(category_name, str) or not category_name.strip():
        raise ResponseContractError(
            f"{context} category name is invalid",
            category="invalid_category_tree",
        )
    return category_id.strip(), category_name.strip()


def _node_children(node: dict[str, Any], *, context: str) -> list[Any]:
    """Validate children only for levels the discovery algorithm consumes."""

    # 根和二级分类必须显式提供列表；三级以下完全不读取。
    children = node.get("children")
    if not isinstance(children, list):
        raise ResponseContractError(
            f"{context} category children are invalid",
            category="invalid_category_tree",
        )
    return children


def _is_excluded_category(category_id: str, category_name: str) -> bool:
    """Return whether one category is an aggregate 'all' node."""

    return category_id == "0" or category_name == "全部"


def parse_category_tree(payload: dict[str, Any]) -> CategoryDiscoveryResult:
    """Return level-three descendants below every non-aggregate level-one node."""

    # 解码后的响应必须仍是 JSON 对象，避免非对象响应触发普通 AttributeError。
    if not isinstance(payload, dict):
        raise ResponseContractError(
            "category response is not an object",
            category="invalid_category_tree",
        )
    # st 必须是严格整数 0，bool 不能冒充平台状态码。
    response_status = payload.get("st")
    if type(response_status) is not int or response_status != 0:
        raise ResponseContractError(
            "category response status is not successful",
            category="category_response_error",
        )
    # 分类发现只从 data.cate_list 枚举一级分类节点。
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ResponseContractError(
            "category response data is not an object",
            category="invalid_category_tree",
        )
    # data.cate_id/cate_name 是页面当前选中项，不得用于根定位。
    top_level_nodes = data.get("cate_list")
    if not isinstance(top_level_nodes, list):
        raise ResponseContractError(
            "category response cate_list is invalid",
            category="invalid_category_tree",
        )
    # 一级 ID 在一次分类响应中必须唯一，名称可以重复但请求使用真实 ID。
    seen_level1_ids: set[str] = set()
    # 三级 ID 在同一批次内必须唯一。
    seen_category_ids: set[str] = set()
    # 有效三级节点按过滤后的原始顺序连续编号。
    discovered_categories: list[DiscoveredCategory] = []
    for level1_node in top_level_nodes:
        level1_id, level1_name = _node_identity(level1_node, context="level-one")
        if _is_excluded_category(level1_id, level1_name):
            continue
        if level1_id in seen_level1_ids:
            raise ResponseContractError(
                "duplicate level-one category id",
                category="invalid_category_tree",
            )
        seen_level1_ids.add(level1_id)
        # 每个一级分类独立校验二级 ID，跨一级重复不影响级联请求。
        seen_level2_ids: set[str] = set()
        level2_nodes = _node_children(level1_node, context="level-one")
        for level2_node in level2_nodes:
            level2_id, level2_name = _node_identity(
                level2_node,
                context="level-two",
            )
            if _is_excluded_category(level2_id, level2_name):
                continue
            if level2_id in seen_level2_ids:
                raise ResponseContractError(
                    "duplicate level-two category id",
                    category="invalid_category_tree",
                )
            seen_level2_ids.add(level2_id)
            # 二级节点的直接 children 就是目标三级分类。
            level3_nodes = _node_children(level2_node, context="level-two")
            for level3_node in level3_nodes:
                level3_id, level3_name = _node_identity(
                    level3_node,
                    context="level-three",
                )
                if _is_excluded_category(level3_id, level3_name):
                    continue
                if level3_id in seen_category_ids:
                    raise ResponseContractError(
                        "duplicate target category id",
                        category="duplicate_category_id",
                    )
                seen_category_ids.add(level3_id)
                discovered_categories.append(
                    DiscoveredCategory(
                        discovery_order=len(discovered_categories) + 1,
                        level1_category_id=level1_id,
                        level1_category_name=level1_name,
                        level2_category_id=level2_id,
                        level2_category_name=level2_name,
                        category_id=level3_id,
                        category_name=level3_name,
                    )
                )
    if not discovered_categories:
        raise CategoryDiscoveryEmptyError()
    return CategoryDiscoveryResult(
        root_category_id=None,
        root_category_name=None,
        categories=tuple(discovered_categories),
    )
