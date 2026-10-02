"""Synthetic flat taxonomy exercises observed columns, not live acceptance."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from compass_collector.errors import ResponseContractError
from compass_collector.platforms.taobao_categories import parse_category_tree
from compass_collector.platforms.taobao_config import TaobaoCategoryScopeConfig

# 合成分支包含乱序的父节点和第四层，验证按关系计算层级。
TREE = {
    "code": 0,
    "data": [
        [2165, 50021853, "香薰蜡烛", 0, "free", "N"],
        [0, 50025705, "洗护清洁剂/卫生巾/纸/香薰", 1],
        [50025705, 2165, "香薰用品", 2],
        [2165, 216502, "香薰精油", 0],
        [50021853, 900, "更深分类", 0],
        [0, 999, "其他行业", 1],
        [50025705, 88, "其它", 0],
        [2165, 89, "全部", 0],
    ],
}


def test_parent_links_define_three_levels_not_cate_flag():
    """Preserve source sibling order while ignoring fourth-level and summary."""
    # 默认范围只采指定根，层级与 cateFlag=0 无关。
    result = parse_category_tree(TREE, TaobaoCategoryScopeConfig())
    assert [scope.key for scope in result.categories] == ["50021853", "216502"]
    assert [scope.discovery_order for scope in result.categories] == [1, 2]
    assert result.categories[0].path == ("洗护清洁剂/卫生巾/纸/香薰", "香薰用品", "香薰蜡烛")
    assert result.categories[0].platform_metadata == {
        "root_category_id": "50025705", "parent_cate_id": "2165",
        "cate_id": "50021853", "cate_flag": "0",
    }


def test_selected_targets_validate_all_and_keep_configured_order():
    """One bad target cannot silently reduce the configured scope."""
    # 指定顺序故意与接口顺序相反。
    selection = TaobaoCategoryScopeConfig(mode="selected", targets=["216502", "50021853"])
    assert [scope.key for scope in parse_category_tree(TREE, selection).categories] == selection.targets
    with pytest.raises(ResponseContractError):
        parse_category_tree(TREE, TaobaoCategoryScopeConfig(mode="selected", targets=["216502", "900"]))


@pytest.mark.parametrize("mutation", ["bool_code", "duplicate", "empty_name", "missing_root", "bad_flag", "non_array"])
def test_invalid_taxonomy_fails_before_collection(mutation):
    """Catch malformed or ambiguous taxonomy at discovery."""
    # 每次使用独立副本，避免参数化用例相互污染。
    payload = deepcopy(TREE)
    if mutation == "bool_code":
        payload["code"] = False
    elif mutation == "duplicate":
        payload["data"].append(payload["data"][0])
    elif mutation == "empty_name":
        payload["data"][0][2] = ""
    elif mutation == "missing_root":
        payload["data"].pop(1)
    elif mutation == "bad_flag":
        payload["data"][0][3] = True
    else:
        payload["data"] = {}
    with pytest.raises(ResponseContractError):
        parse_category_tree(payload, TaobaoCategoryScopeConfig())


@pytest.mark.parametrize("selection", [
    {"mode": "selected"}, {"targets": ["216502"]},
    {"mode": "selected", "targets": ["216502", "216502"]},
    {"mode": "selected", "targets": ["0"]}, {"root_category_id": "missing"},
    {"target_level": 4},
])
def test_scope_configuration_is_strict(selection):
    """Reject unsupported scope settings without opening Chrome."""
    with pytest.raises(ValidationError):
        TaobaoCategoryScopeConfig(**selection)
