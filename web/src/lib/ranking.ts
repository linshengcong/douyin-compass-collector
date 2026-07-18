import type { RankingRecord } from "../types";

/** 将中文金额或件数区间转换为可排序、可筛选的最低数值。 */
export function metricLowerBound(value: string): number {
  const matched = value.replaceAll("¥", "").match(/([\d.]+)(亿|万)?/);
  if (!matched) return 0;
  const multiplier = matched[2] === "亿" ? 100_000_000 : matched[2] === "万" ? 10_000 : 1;
  return Number(matched[1]) * multiplier;
}

/** 生成同一层级且保持原数据出现顺序的可选项。 */
export function uniqueOptions(records: RankingRecord[], key: keyof RankingRecord): string[] {
  return ["全部", ...Array.from(new Set(records.map((record) => String(record[key]))))];
}

/** 将数值筛选值转换为移动端 input 的可读摘要。 */
export function thresholdLabel(value: string, unit: string): string {
  return Number(value) > 0 ? `≥ ${Number(value).toLocaleString()} ${unit}` : "不限";
}
