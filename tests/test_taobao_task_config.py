"""Platform selection must enforce independent Taobao and Compass contracts."""

import pytest
from pydantic import ValidationError

from compass_collector.config import TaskConfig
from compass_collector.platforms.taobao_config import TaobaoCategoryScopeConfig, TaobaoRankConfig


def taobao_task(**changes):
    """Build one task dictionary without accepting guessed business options."""
    # 共用字段保持现有 YAML 形状，业务字段由明确的平台决定默认值。
    values = {"id": "taobao_household_cleaning_realtime", "platform": "taobao", "display_name": "淘宝实时榜", "schedule": "0 14 * * *"}
    values.update(changes)
    return values


def test_declared_taobao_platform_selects_all_business_defaults():
    """A default Taobao task cannot receive the default Compass rank model."""
    # 直接验证生产任务入口，而非仅验证分离的业务配置类。
    task = TaskConfig.model_validate(taobao_task())
    assert isinstance(task.rank, TaobaoRankConfig)
    assert isinstance(task.category_scope, TaobaoCategoryScopeConfig)
    assert task.rank.page_size == 20 and task.rank.rank_type == "gmv"
    assert task.category_scope.root_category_id == "50025705"
    assert task.date.date_type == "today"
    assert task.filters.seller_type == -1
    assert TaskConfig.model_validate(task.model_dump()) == task


@pytest.mark.parametrize("field,value", [
    ("rank", {"page_size": 10}),
    ("rank", {"endpoint_path": "/mc/mq/mkt/item/offline/rank.json"}),
    ("date", {"date_type": 1}),
    ("filters", {"brand_type": -1}),
    ("category_scope", {"industry_id": "5"}),
    ("category_scope", {"mode": "selected", "targets": ["١٢٣"]}),
])
def test_taobao_rejects_foreign_or_unsupported_business_fields(field, value):
    """Union parsing may not silently choose a different platform's model."""
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(taobao_task(**{field: value}))


def test_unknown_platform_and_compass_with_taobao_fields_are_rejected():
    """Task selection itself rejects unsupported identities and mixed models."""
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(taobao_task(platform="unknown"))
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(taobao_task(platform="compass", category_scope={"root_category_id": "50025705"}, filters={}, date={"date_type": "today"}))
