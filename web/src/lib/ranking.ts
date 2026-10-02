import { MetricOperator, RankingPlatform, SortDirection, SortField, type RankingFilters, type RankingRecord } from "../types";

/** 原始区间的下界只用于比较，未知指标不能被转换成零。 */
export function metricLowerBound(value: string | null | undefined): number | null {
  if (!value || value.trim() === "-") return null;
  // 两平台有效展示格式均以数值或货币符号开头；拒绝尾随垃圾。
  const matched = value.trim().match(/^¥?(\d+(?:\.\d+)?)(亿|万)?(?:\s*[~-]\s*¥?\d+(?:\.\d+)?(?:亿|万)?)?$/);
  if (!matched) return null;
  // 量级转换保留真实金额或人数口径，不改变原文展示。
  const multiplier = matched[2] === "亿" ? 100_000_000 : matched[2] === "万" ? 10_000 : 1;
  const result = Number(matched[1]) * multiplier;
  return Number.isFinite(result) ? result : null;
}

/** 按平台选取业务指标，淘宝优先使用发布端已解析的人数下界。 */
export function rankingMetric(record: RankingRecord, field: SortField): number | null {
  if (field === SortField.排名) return record.rank;
  if (record.platform === RankingPlatform.淘宝) {
    // 排序字段只来自平台可用选项；不把抖音字段当淘宝人数读取。
    const bound = field === SortField.支付买家数 ? record.pay_buyer_count_min_value : field === SortField.访客数 ? record.visitor_count_min_value : null;
    return bound === null || bound === undefined ? null : Number(bound);
  }
  return metricLowerBound(field === SortField.用户支付金额 ? record.pay_amount : field === SortField.成交件数 ? record.pay_combo_count : null);
}

/** 空输入表示没有该侧阈值；启用数值条件时未知指标不参加结果。 */
export function matchesMetricFilter(value: number | null, operator: MetricOperator, minimum: string, maximum: string): boolean {
  if (operator === MetricOperator.不限) return true;
  if (value === null || !Number.isFinite(value)) return false;
  // 不将空字符串或无效输入当成业务零阈值。
  const min = minimum.trim() === "" ? null : Number(minimum);
  const max = maximum.trim() === "" ? null : Number(maximum);
  if (operator === MetricOperator.大于等于) return min === null || !Number.isFinite(min) || value >= min;
  if (operator === MetricOperator.小于等于) return max === null || !Number.isFinite(max) || value <= max;
  return (min === null || !Number.isFinite(min) || value >= min) && (max === null || !Number.isFinite(max) || value <= max);
}

/** 桌面实际筛选与移动弹层预览共享同一口径，避免计数与列表不一致。 */
export function filterRankingRecords(records: RankingRecord[], filters: RankingFilters, platform: RankingPlatform): RankingRecord[] {
  // 每次只读取当前平台的两项指标，关键词保持现有大小写不敏感行为。
  const keyword = filters.keyword.trim().toLocaleLowerCase();
  const primary = platform === RankingPlatform.淘宝 ? SortField.支付买家数 : SortField.用户支付金额;
  const secondary = platform === RankingPlatform.淘宝 ? SortField.访客数 : SortField.成交件数;
  return records.filter((item) => (!keyword || item.product_name.toLocaleLowerCase().includes(keyword) || item.shop_name.toLocaleLowerCase().includes(keyword))
    && (filters.level1 === "全部" || item.level1 === filters.level1)
    && (filters.level2 === "全部" || item.level2 === filters.level2)
    && (filters.level3 === "全部" || item.level3 === filters.level3)
    && (!filters.newOnly || item.newly_on_ranking === true)
    && matchesMetricFilter(rankingMetric(item, primary), filters.payOperator, filters.payMinimum, filters.payMaximum)
    && matchesMetricFilter(rankingMetric(item, secondary), filters.countOperator, filters.countMinimum, filters.countMaximum));
}

/** 排序保持未知值在最后，升降序都不能把缺失误当最大或最小人数。 */
export function sortRankingRecords(records: RankingRecord[], filters: RankingFilters): RankingRecord[] {
  return [...records].sort((left, right) => {
    // 同值使用稳定原顺序，保留分类与商品的发布顺序。
    const a = rankingMetric(left, filters.sortField);
    const b = rankingMetric(right, filters.sortField);
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return filters.sortDirection === SortDirection.升序 ? a - b : b - a;
  });
}

/** 平台决定可见业务排序字段，不能展示另一平台的指标。 */
export function platformSortFields(platform: RankingPlatform): SortField[] {
  return platform === RankingPlatform.淘宝 ? [SortField.排名, SortField.支付买家数, SortField.访客数] : [SortField.排名, SortField.用户支付金额, SortField.成交件数];
}

/** 生成同一层级且保持原数据出现顺序的可选项。 */
export function uniqueOptions(records: RankingRecord[], key: keyof RankingRecord): string[] {
  return ["全部", ...Array.from(new Set(records.map((record) => String(record[key]))))];
}

/** 将数值筛选值转换为移动端 input 的可读摘要。 */
export function thresholdLabel(value: string, unit: string): string {
  return Number(value) > 0 ? `≥ ${Number(value).toLocaleString()} ${unit}` : "不限";
}
