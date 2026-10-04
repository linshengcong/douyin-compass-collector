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


def additional_root_payload():
    """构造含目标三分支和额外兄弟分支的合成分类树，避免依赖真实数据。"""
    # 每个目标分支都有三级节点，另保留一个未配置的男士理容分支。
    payload = deepcopy(TREE)
    payload["data"].extend([
        [0, 50016348, "家庭/个人清洁工具", 1],
        [50016348, 2132, "卫浴/置物用具", 2],
        [2132, 9101, "浴帘", 0],
        [2132, 9102, "皂盒", 0],
        [50016348, 50003949, "家务/地板清洁用具", 2],
        [50003949, 9201, "拖把 > 平板拖把", 0],
        [50016348, 50009146, "个人洗护清洁用具", 2],
        [50009146, 9301, "浴帽", 0],
        [50016348, 9400, "男士理容工具", 2],
        [9400, 9401, "理容工具", 0],
        [9101, 9501, "不采四级", 0],
    ])
    return payload


def test_additional_root_keeps_main_scope_and_only_selected_branches():
    """跨根追加保持主根、分支顺序、完整路径和连续编号，不扩展到兄弟分支。"""
    # 故意与树顺序不同，验证追加分支严格按配置顺序展开。
    selection = TaobaoCategoryScopeConfig(additional_roots=[{
        "root_category_id": "50016348", "level2_category_ids": ["50009146", "2132"],
    }])
    # 正式解析器处理完整合成树，不能靠固定的已解析对象绕过归属验证。
    result = parse_category_tree(additional_root_payload(), selection)
    assert result.root_category_id is None and result.root_category_name is None
    assert [scope.key for scope in result.categories] == ["50021853", "216502", "9301", "9101", "9102"]
    assert [scope.discovery_order for scope in result.categories] == [1, 2, 3, 4, 5]
    assert result.categories[2].path == ("家庭/个人清洁工具", "个人洗护清洁用具", "浴帽")
    assert result.categories[2].platform_metadata == {
        "root_category_id": "50016348", "parent_cate_id": "50009146",
        "cate_id": "9301", "cate_flag": "0",
    }
    assert selection == TaobaoCategoryScopeConfig.model_validate(selection.model_dump())


@pytest.mark.parametrize("root_id,branch_id", [
    ("123456", "2132"), ("2132", "9101"), ("50016348", "2165"),
    ("50016348", "9101"), ("50016348", "123456"),
])
def test_additional_root_rejects_missing_or_wrong_level_scope(root_id, branch_id):
    """根、二级分支和归属必须全部存在，禁止静默遗漏或误采。"""
    # 只修改范围身份，保留其余正常分类以证明局部错误会阻断整个发现。
    selection = TaobaoCategoryScopeConfig(additional_roots=[{
        "root_category_id": root_id, "level2_category_ids": [branch_id],
    }])
    with pytest.raises(ResponseContractError) as caught:
        parse_category_tree(additional_root_payload(), selection)
    assert caught.value.category == "invalid_configured_category"


@pytest.mark.parametrize("mutation", ["empty_branch", "duplicate_branch", "duplicate_leaf"])
def test_additional_root_rejects_empty_and_ambiguous_branches(mutation):
    """任一追加分支为空或身份歧义时不能只返回其他成功分支。"""
    # 独立树副本只在本测试中注入无效节点。
    payload = additional_root_payload()
    if mutation == "empty_branch":
        payload["data"] = [row for row in payload["data"] if row[0] != 2132]
    elif mutation == "duplicate_branch":
        payload["data"].append([50016348, 2132, "重复二级", 2])
    else:
        payload["data"].append([50009146, 9101, "重复三级", 0])
    # 第二个有效分支不能掩盖第一个无效分支。
    selection = TaobaoCategoryScopeConfig(additional_roots=[{
        "root_category_id": "50016348", "level2_category_ids": ["2132", "50009146"],
    }])
    with pytest.raises(ResponseContractError):
        parse_category_tree(payload, selection)


@pytest.mark.parametrize("changes", [
    {"additional_roots": [{"root_category_id": "50016348", "level2_category_ids": []}]},
    {"additional_roots": [{"root_category_id": "50016348", "level2_category_ids": ["0"]}]},
    {"additional_roots": [{"root_category_id": "50016348", "level2_category_ids": ["2132", "2132"]}]},
    {"additional_roots": [{"root_category_id": "50025705", "level2_category_ids": ["2165"]}]},
    {"additional_roots": [{"root_category_id": "50016348", "level2_category_ids": ["2132"]}] * 2},
    {"mode": "selected", "targets": ["50021853"], "additional_roots": [
        {"root_category_id": "50016348", "level2_category_ids": ["2132"]},
    ]},
])
def test_additional_root_configuration_rejects_ambiguous_selections(changes):
    """配置加载阶段拒绝重复根、重复分支和指定三级模式的混用。"""
    with pytest.raises(ValidationError):
        TaobaoCategoryScopeConfig(**changes)
