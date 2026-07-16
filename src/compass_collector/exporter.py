"""CSV presentation formatting and atomic export staging."""

import csv
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compass_collector.errors import PublicationError
from compass_collector.models import MetricRange, ProductRankEntry


# CSV 字段顺序是已确认的业务展示契约。
CSV_HEADERS = ("排名", "商品", "店铺名称", "用户支付金额", "成交件数", "首次上榜")
# 平台 price 原值除以 100 得到元。
PRICE_SCALE = Decimal(100)
# 平台 number 原值除以 10 得到件数。
NUMBER_SCALE = Decimal(10)
# 中文紧凑展示使用万和亿两个量级。
TEN_THOUSAND = Decimal(10_000)
HUNDRED_MILLION = Decimal(100_000_000)


def format_decimal(value: Decimal) -> str:
    """Render a decimal without scientific notation or meaningless trailing zeros."""

    # 定点字符串用于安全删除小数末尾 0。
    fixed_value = format(value, "f")
    if "." in fixed_value:
        fixed_value = fixed_value.rstrip("0").rstrip(".")
    return fixed_value


def format_compact_value(value: Decimal) -> str:
    """Format one non-negative display value with a Chinese compact suffix."""

    if value >= HUNDRED_MILLION:
        # 亿级数值除以一亿后保留必要小数。
        scaled_value = value / HUNDRED_MILLION
        return f"{format_decimal(scaled_value)}亿"
    if value >= TEN_THOUSAND:
        # 万级数值除以一万后保留必要小数。
        scaled_value = value / TEN_THOUSAND
        return f"{format_decimal(scaled_value)}万"
    return format_decimal(value)


def format_metric_range(metric_range: MetricRange) -> str:
    """Convert one raw platform range into the agreed CSV display format."""

    if metric_range.unit == "price":
        # 金额上下界分别从原值换算为元。
        minimum = Decimal(metric_range.min_value) / PRICE_SCALE
        maximum = Decimal(metric_range.max_value) / PRICE_SCALE
        return f"¥{format_compact_value(minimum)}-¥{format_compact_value(maximum)}"
    if metric_range.unit == "number":
        # 成交件数上下界分别从平台原值换算。
        minimum = Decimal(metric_range.min_value) / NUMBER_SCALE
        maximum = Decimal(metric_range.max_value) / NUMBER_SCALE
        return f"{format_compact_value(minimum)}-{format_compact_value(maximum)}"
    raise PublicationError(
        "unsupported metric unit for CSV",
        category="csv_format_error",
    )


@dataclass(slots=True)
class StagedCsvExport:
    """Own one temporary CSV until the database transaction publishes it."""

    temporary_path: Path
    final_path: Path
    published: bool = False

    def publish(self) -> None:
        """Atomically publish the complete temporary CSV."""

        if self.final_path.exists():
            raise PublicationError(
                "CSV target already exists",
                category="csv_conflict",
            )
        os.replace(self.temporary_path, self.final_path)
        self.published = True

    def rollback(self) -> None:
        """Remove only files owned by this failed publication attempt."""

        if self.temporary_path.exists():
            self.temporary_path.unlink()
        if self.published and self.final_path.exists():
            self.final_path.unlink()


class CsvExporter:
    """Create one product-per-row CSV without mutating domain records."""

    def __init__(self, export_root: Path) -> None:
        """Store the configured runtime export root."""

        # 导出根目录默认位于 runtime/exports。
        self.export_root = export_root

    def prepare(
        self,
        *,
        task_id: str,
        planned_at: datetime,
        version: int,
        run_id: str,
        entries: tuple[ProductRankEntry, ...],
    ) -> StagedCsvExport:
        """Write a complete UTF-8 BOM CSV to a temporary file."""

        # 业务日期目录使用计划时间所属日期。
        export_directory = self.export_root / planned_at.date().isoformat()
        export_directory.mkdir(parents=True, exist_ok=True)
        # v1 不显式加后缀，强制重采从 v2 开始标记。
        version_suffix = "" if version == 1 else f"_v{version}"
        # 文件名包含计划时分，与工程方案示例一致。
        final_path = export_directory / (
            f"{task_id}_{planned_at.strftime('%H%M')}{version_suffix}.csv"
        )
        # 临时文件包含 run_id，避免并行调试名称冲突。
        temporary_path = final_path.with_name(f".{final_path.name}.{run_id}.tmp")
        try:
            with temporary_path.open("w", encoding="utf-8-sig", newline="") as file_handle:
                # CSV writer 由标准库处理逗号、换行和引号转义。
                writer = csv.writer(file_handle)
                writer.writerow(CSV_HEADERS)
                for entry in sorted(entries, key=lambda item: item.rank):
                    # 店铺名称严格按接口原始顺序拼接。
                    shop_names = " | ".join(shop.shop_name for shop in entry.shops)
                    writer.writerow(
                        (
                            entry.rank,
                            entry.product_name,
                            shop_names,
                            format_metric_range(entry.pay_amount),
                            format_metric_range(entry.pay_combo_count),
                            "true" if entry.newly_on_ranking else "false",
                        )
                    )
        except Exception as error:
            if temporary_path.exists():
                temporary_path.unlink()
            if isinstance(error, PublicationError):
                raise
            raise PublicationError(
                "failed to prepare CSV",
                category="csv_write_error",
            ) from error
        return StagedCsvExport(
            temporary_path=temporary_path,
            final_path=final_path,
        )
