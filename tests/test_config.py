"""Configuration contract tests."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from compass_collector.config import AppConfig, load_config


# 真实项目配置是合法配置契约的基线。
CONFIG_PATH = Path("config/tasks.yaml")


def test_real_config_is_valid() -> None:
    """Load the checked-in dynamic category configuration successfully."""

    # 加载后的应用配置用于核对动态三级分类契约。
    config = load_config(CONFIG_PATH)
    assert config.http.concurrency == 1
    assert config.http.request_interval_seconds.min == 0.1
    assert config.http.request_interval_seconds.max == 0.3
    # 首个任务是当前唯一启用的全一级分类任务。
    task = config.tasks[0]
    assert task.id == "product_hot_sale_all_level3"
    assert task.display_name == "全行业三级分类商品实时榜"
    assert task.category_scope.mode == "all_level1"
    assert task.category_scope.target_level == 3
    assert task.category_scope.exclude_all is True
    assert task.filters.brand_type == 0
    assert task.filters.price_bin == "10001-?"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("brand_type", 1),
        ("price_bin", "10000-?"),
    ],
)
def test_unverified_ranking_filters_are_rejected(
    field_name: str,
    invalid_value: object,
) -> None:
    """Reject brand and price values that have not been verified by a real request."""

    # 真实配置只替换一个筛选字段，以锁定当前已验证的平台契约。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["tasks"][0]["filters"][field_name] = invalid_value

    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


def test_unknown_config_field_is_rejected() -> None:
    """Reject a misspelled or unsupported field before Chrome starts."""

    # 真实 YAML 用于构造只增加一个未知字段的配置。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["http"]["unexpected_retry"] = True
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("mode", "dynamic_descendants"),
        ("target_level", 4),
        ("exclude_all", False),
    ],
)
def test_unsupported_category_scope_is_rejected(
    field_name: str,
    invalid_value: object,
) -> None:
    """Reject category modes that would change the agreed level-three contract."""

    # 真实 YAML 用于构造一个被严格契约禁止的分类范围。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["tasks"][0]["category_scope"][field_name] = invalid_value
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


def test_legacy_fixed_category_and_pagination_are_rejected() -> None:
    """Do not silently retain the removed fixed-category or item-cap contract."""

    # 旧分类和分页字段用于证明新基线没有兼容分支。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["tasks"][0]["filters"]["category"] = {
        "id": 1000001823,
        "name": "水饮冲调",
    }
    raw_config["tasks"][0]["pagination"] = {"max_items": 200}

    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


def test_legacy_single_root_configuration_is_rejected_before_browser_start() -> None:
    """Reject the removed single-root field instead of silently narrowing scope."""

    # 旧 root 字段属于未知配置，必须在启动 Chrome 前失败。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["tasks"][0]["category_scope"]["root"] = {"name": "食品饮料"}

    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)
