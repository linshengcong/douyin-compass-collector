"""Dynamic level-three category discovery tests using one sanitized fixture."""

import json
from pathlib import Path
from typing import Any

import pytest

from compass_collector.category_discovery import parse_category_tree
from compass_collector.errors import ResponseContractError


# 精简 Fixture 只保留分类发现会消费的真实响应形状。
FIXTURE_PATH = Path("tests/fixtures/category_tree.json")


def load_category_fixture() -> dict[str, Any]:
    """Load a fresh category-tree payload so each test can mutate it safely."""

    # 每次重新解析 JSON，避免测试之间共享可变分类节点。
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def find_food_root(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the fixture's food level-one node for focused contract mutations."""

    # Fixture 包含多个一级分类，食品节点仍按名称定位以便局部破坏。
    return next(
        node
        for node in payload["data"]["cate_list"]
        if node["cate_name"] == "食品饮料"
    )


def assert_contract_error(
    payload: Any,
    *,
    expected_category: str,
) -> None:
    """Assert one malformed category payload fails with a stable safe category."""

    # 稳定错误分类会进入批次状态、Manifest 与通知汇总。
    with pytest.raises(ResponseContractError) as error_info:
        parse_category_tree(payload)
    assert error_info.value.category == expected_category


def test_discovers_all_level_one_categories_in_original_path_order() -> None:
    """Preserve filtered L1-to-L3 API order and complete category paths."""

    # data.cate_name 指向二级分类，解析器仍只从 cate_list 枚举全部一级分类。
    payload = load_category_fixture()
    # 分类发现结果包含两个一级分类下排除汇总节点后的四个三级分类。
    result = parse_category_tree(payload)

    assert result.root_category_id is None
    assert result.root_category_name is None
    assert [category.discovery_order for category in result.categories] == [1, 2, 3, 4]
    assert [category.category_id for category in result.categories] == [
        "fixture-level3-dresses",
        "fixture-level3-biscuit",
        "fixture-level3-seafood",
        "fixture-level3-tea",
    ]
    assert [category.display_path for category in result.categories] == [
        "服饰内衣 > 女装 > 连衣裙",
        "食品饮料 > 休闲食品 > 饼干",
        "食品饮料 > 休闲食品 > 海味零食",
        "食品饮料 > 水饮冲调 > 茶叶",
    ]


def test_excludes_categories_by_zero_id_or_all_name() -> None:
    """Exclude aggregate nodes when either verified marker identifies them."""

    # Fixture 同时包含 ID 为 0 但名称非“全部”、名称为“全部”但 ID 非 0。
    payload = load_category_fixture()
    # 有效分类集合用于确认两个 OR 分支都被排除。
    result = parse_category_tree(payload)
    # 解析结果分别提取 ID 和名称，避免依赖对象显示格式。
    category_ids = {category.category_id for category in result.categories}
    category_names = {category.category_name for category in result.categories}

    assert "0" not in category_ids
    assert "fixture-all-by-name" not in category_ids
    assert "全部" not in category_names
    assert "二级分类汇总" not in category_names


def test_strictly_ignores_level_four_and_deeper_nodes() -> None:
    """Stop at nesting level three even when deeper descendants exist or drift."""

    # 三级“海味零食”在真实形状中仍带四、五级 children。
    payload = load_category_fixture()
    # 将四级入口改成无效类型，证明解析器不会读取三级以下结构。
    food_root = find_food_root(payload)
    food_root["children"][0]["children"][2]["children"] = "ignored-level-four"
    # 目标三级节点仍应被正常发现。
    result = parse_category_tree(payload)

    assert [category.category_name for category in result.categories] == [
        "连衣裙",
        "饼干",
        "海味零食",
        "茶叶",
    ]


def test_renamed_level_one_category_is_discovered_without_configuration_change() -> None:
    """Use the current API name instead of requiring a configured root name."""

    # 一级名称变化只改变当次路径快照，不再导致根名称告警。
    payload = load_category_fixture()
    find_food_root(payload)["cate_name"] = "食品分类已改名"

    result = parse_category_tree(payload)

    assert result.categories[1].level1_category_name == "食品分类已改名"


def test_duplicate_level_one_id_is_rejected() -> None:
    """Reject duplicate top-level IDs before any ranking request."""

    # 第二个一级节点复用食品 ID，导致 industry_id 归属不可信。
    payload = load_category_fixture()
    payload["data"]["cate_list"].append(
        {
            "cate_id": "13",
            "cate_name": "重复行业",
            "children": [],
        }
    )

    assert_contract_error(payload, expected_category="invalid_category_tree")


def test_zero_level_three_categories_across_all_roots_is_rejected() -> None:
    """Reject a response where no level-one category yields a target category."""

    # 唯一二级节点只包含一个“全部”汇总项。
    payload = load_category_fixture()
    # 服饰一级分类也清空，确保全范围最终没有三级分类。
    payload["data"]["cate_list"][0]["children"] = []
    find_food_root(payload)["children"] = [
        {
            "cate_id": "fixture-empty-level2",
            "cate_name": "空二级分类",
            "children": [
                {
                    "cate_id": "0",
                    "cate_name": "全部",
                    "children": [],
                }
            ],
        }
    ]

    assert_contract_error(payload, expected_category="category_discovery_empty")


def test_duplicate_level_three_id_is_rejected() -> None:
    """Reject duplicate target IDs before category-run database creation."""

    # 将不同二级路径下的三级分类改成同一平台 ID。
    payload = load_category_fixture()
    food_root = find_food_root(payload)
    food_root["children"][1]["children"][2]["cate_id"] = (
        "fixture-level3-biscuit"
    )

    assert_contract_error(payload, expected_category="duplicate_category_id")


@pytest.mark.parametrize(
    ("malformed_case", "expected_category"),
    [
        ("business_status", "category_response_error"),
        ("data_type", "invalid_category_tree"),
        ("cate_list_type", "invalid_category_tree"),
        ("root_children_type", "invalid_category_tree"),
        ("level2_children_type", "invalid_category_tree"),
        ("level3_identity", "invalid_category_tree"),
    ],
)
def test_rejects_consumed_category_tree_structure_errors(
    malformed_case: str,
    expected_category: str,
) -> None:
    """Reject malformed envelope and every consumed category-tree level."""

    # 每个 case 只破坏当前解析算法实际消费的一处结构。
    payload = load_category_fixture()
    if malformed_case == "business_status":
        # 非零业务状态不能进入分类树解析。
        payload["st"] = 1
    elif malformed_case == "data_type":
        # data 必须保持 JSON 对象结构。
        payload["data"] = []
    elif malformed_case == "cate_list_type":
        # 顶层分类集合必须是数组。
        payload["data"]["cate_list"] = {}
    elif malformed_case == "root_children_type":
        # 任意被消费的一级分类必须提供二级分类数组。
        find_food_root(payload)["children"] = {}
    elif malformed_case == "level2_children_type":
        # 被消费的二级分类必须提供三级分类数组。
        find_food_root(payload)["children"][0]["children"] = {}
    else:
        # 被消费的三级分类必须包含非空字符串 ID。
        find_food_root(payload)["children"][0]["children"][1].pop("cate_id")

    assert_contract_error(payload, expected_category=expected_category)


def test_non_object_payload_is_rejected_as_contract_error() -> None:
    """Turn a decoded non-object JSON response into a safe contract failure."""

    # 非对象响应不能泄漏为普通 AttributeError。
    assert_contract_error([], expected_category="invalid_category_tree")
