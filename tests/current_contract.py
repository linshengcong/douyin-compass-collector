"""Shared test contract values derived from the checked-in runtime config."""

from pathlib import Path

from compass_collector.config import load_config


# 真实项目配置是测试契约的单一来源，避免在多个测试文件重复手写默认值。
CONFIG_PATH = Path("config/tasks.yaml")
# 当前应用配置只在测试进程内加载一次，供契约测试复用。
CURRENT_CONFIG = load_config(CONFIG_PATH)
# 当前唯一启用任务代表默认采集任务的筛选和分页契约。
CURRENT_TASK = CURRENT_CONFIG.tasks[0]
# 当前 HTTP 间隔配置用于测试请求节流和文档描述。
CURRENT_INTERVAL = CURRENT_CONFIG.http.request_interval_seconds
# 当前最小请求间隔来自真实 YAML。
CURRENT_INTERVAL_MIN = CURRENT_INTERVAL.min
# 当前最大请求间隔来自真实 YAML。
CURRENT_INTERVAL_MAX = CURRENT_INTERVAL.max
# 文档中的请求间隔显示格式必须和当前 YAML 保持一致。
CURRENT_INTERVAL_LABEL = f"{CURRENT_INTERVAL_MIN:g}～{CURRENT_INTERVAL_MAX:g} 秒"
# 当前默认品牌筛选值来自真实 YAML。
CURRENT_BRAND_TYPE = CURRENT_TASK.filters.brand_type
# 当前默认价格带筛选值来自真实 YAML。
CURRENT_PRICE_BIN = CURRENT_TASK.filters.price_bin
# 文档必须显式写出当前价格带请求参数，避免描述和 YAML 偏离。
CURRENT_PRICE_BIN_DOC_MARKER = f"price_bin={CURRENT_PRICE_BIN}"
