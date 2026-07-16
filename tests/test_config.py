"""Configuration contract tests."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from compass_collector.config import AppConfig, load_config


# 真实项目配置是合法配置契约的基线。
CONFIG_PATH = Path("config/tasks.yaml")


def test_real_config_is_valid() -> None:
    """Load the checked-in stage-one configuration successfully."""

    # 加载后的应用配置用于核对已确认的固定值。
    config = load_config(CONFIG_PATH)
    assert config.http.concurrency == 1
    assert config.tasks[0].pagination.max_items == 200


def test_unknown_config_field_is_rejected() -> None:
    """Reject a misspelled or unsupported field before Chrome starts."""

    # 真实 YAML 用于构造只增加一个未知字段的配置。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["http"]["unexpected_retry"] = True
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


def test_non_page_aligned_max_items_is_rejected() -> None:
    """Reject an item cap that would stop inside a fixed ten-item page."""

    # 真实 YAML 用于构造一个不满足页对齐约束的配置。
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["tasks"][0]["pagination"]["max_items"] = 15
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)
