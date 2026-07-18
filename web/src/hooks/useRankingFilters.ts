import { useEffect, useMemo, useState } from "react";
import { metricLowerBound, uniqueOptions } from "../lib/ranking";
import { MetricOperator, SortDirection, SortField, type RankingFilters, type RankingRecord } from "../types";

/** 共享的默认筛选条件，重置时恢复为首次上榜与排名升序。 */
export const DEFAULT_RANKING_FILTERS: RankingFilters = {
  keyword: "",
  level1: "全部",
  level2: "全部",
  level3: "全部",
  newOnly: true,
  payMinimum: "",
  payMaximum: "",
  payOperator: MetricOperator.不限,
  countMinimum: "",
  countMaximum: "",
  countOperator: MetricOperator.不限,
  sortField: SortField.排名,
  sortDirection: SortDirection.升序,
};

/** 管理桌面与移动端共享的级联筛选、排序和防抖关键词。 */
export function useRankingFilters(records: RankingRecord[], debounceMs: number) {
  // filters 是两个视图唯一的业务筛选来源，视图组件不保留第二份已确认状态。
  const [filters, setFilters] = useState<RankingFilters>(DEFAULT_RANKING_FILTERS);
  const [effectiveKeyword, setEffectiveKeyword] = useState(filters.keyword);

  useEffect(() => {
    // 移动端输入采用短防抖，桌面端传 0 即保持即时筛选。
    const timer = window.setTimeout(() => setEffectiveKeyword(filters.keyword), debounceMs);
    return () => window.clearTimeout(timer);
  }, [debounceMs, filters.keyword]);

  const level1Options = useMemo(() => uniqueOptions(records, "level1"), [records]);
  const level2Options = useMemo(() => {
    const scoped = filters.level1 === "全部" ? records : records.filter((item) => item.level1 === filters.level1);
    return uniqueOptions(scoped, "level2");
  }, [filters.level1, records]);
  const level3Options = useMemo(() => {
    const scoped = records.filter(
      (item) =>
        (filters.level1 === "全部" || item.level1 === filters.level1) &&
        (filters.level2 === "全部" || item.level2 === filters.level2),
    );
    return uniqueOptions(scoped, "level3");
  }, [filters.level1, filters.level2, records]);

  const filteredRecords = useMemo(() => {
    const normalizedKeyword = effectiveKeyword.trim().toLocaleLowerCase();
    const filtered = records.filter((item) => {
      const hasKeyword = !normalizedKeyword || item.product_name.toLocaleLowerCase().includes(normalizedKeyword) || item.shop_name.toLocaleLowerCase().includes(normalizedKeyword);
      return hasKeyword
        && (filters.level1 === "全部" || item.level1 === filters.level1)
        && (filters.level2 === "全部" || item.level2 === filters.level2)
        && (filters.level3 === "全部" || item.level3 === filters.level3)
        && (!filters.newOnly || item.newly_on_ranking)
        && matchesMetricFilter(metricLowerBound(item.pay_amount), filters.payOperator, filters.payMinimum, filters.payMaximum)
        && matchesMetricFilter(metricLowerBound(item.pay_combo_count), filters.countOperator, filters.countMinimum, filters.countMaximum);
    });
    return filtered.sort((left, right) => {
      const leftValue = filters.sortField === SortField.排名 ? left.rank : metricLowerBound(filters.sortField === SortField.用户支付金额 ? left.pay_amount : left.pay_combo_count);
      const rightValue = filters.sortField === SortField.排名 ? right.rank : metricLowerBound(filters.sortField === SortField.用户支付金额 ? right.pay_amount : right.pay_combo_count);
      return filters.sortDirection === SortDirection.升序 ? leftValue - rightValue : rightValue - leftValue;
    });
  }, [effectiveKeyword, filters, records]);

  /** 更新一级类目时同步将下级筛选恢复为“全部”。 */
  const selectLevel1 = (level1: string) => setFilters((current) => ({ ...current, level1, level2: "全部", level3: "全部" }));
  /** 更新二级类目时同步将三级筛选恢复为“全部”。 */
  const selectLevel2 = (level2: string) => setFilters((current) => ({ ...current, level2, level3: "全部" }));
  /** 更新三级类目不影响上级筛选。 */
  const selectLevel3 = (level3: string) => setFilters((current) => ({ ...current, level3 }));
  /** 将全部筛选恢复为以“仅首次上榜”为核心的默认状态。 */
  const resetFilters = () => setFilters(DEFAULT_RANKING_FILTERS);

  return { filters, setFilters, filteredRecords, level1Options, level2Options, level3Options, selectLevel1, selectLevel2, selectLevel3, resetFilters };
}

/** 根据比较方式判断数值是否落在当前筛选条件内，空值按无限制处理。 */
function matchesMetricFilter(value: number, operator: MetricOperator, minimum: string, maximum: string) {
  const min = Number(minimum);
  const max = Number(maximum);
  if (operator === MetricOperator.大于等于) return !Number.isFinite(min) || value >= min;
  if (operator === MetricOperator.小于等于) return !Number.isFinite(max) || value <= max;
  if (operator === MetricOperator.区间) return (!Number.isFinite(min) || value >= min) && (!Number.isFinite(max) || value <= max);
  return true;
}
