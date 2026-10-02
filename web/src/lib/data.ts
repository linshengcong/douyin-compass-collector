import { decodeHTMLStrict } from "entities";
import { RankingPlatform, type DataSnapshot, type LatestIndex, type RankingRecord } from "../types";

/** URL 只允许正常网页资源与商品跳转，不接受凭据或可执行协议。 */
export function safePublicUrl(value: unknown): string {
  if (typeof value !== "string") throw new Error("invalid public URL");
  // 新发布器输出绝对地址，旧抖音快照也使用 OSS 绝对地址。
  const parsed = new URL(value);
  if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password || !parsed.hostname) throw new Error("invalid public URL");
  return value;
}

/** 核对快照身份并兼容旧抖音数据，错误快照不能进入筛选或渲染。 */
export function decodeRankingBundle(latest: LatestIndex, snapshot: DataSnapshot, platform: RankingPlatform) {
  if (!latest || !snapshot || !Array.isArray(snapshot.records)) throw new Error("invalid snapshot");
  // 旧版本只兼容抖音；淘宝必须来自带独立字段的新版本。
  const version = latest.schema_version ?? 1;
  if (![1, 2, 3].includes(version) || (platform === RankingPlatform.淘宝 && version !== 3)) throw new Error("unsupported schema");
  if ((snapshot.schema_version ?? 1) !== version) throw new Error("schema mismatch");
  if ((latest.platform ?? RankingPlatform.抖音) !== platform || (snapshot.platform ?? RankingPlatform.抖音) !== platform) throw new Error("platform mismatch");
  if (typeof latest.batch_id !== "string" || !latest.batch_id || snapshot.batch_id !== latest.batch_id || snapshot.task_id !== latest.task_id) throw new Error("identity mismatch");
  if (typeof latest.business_date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(latest.business_date) || typeof latest.published_at !== "string" || !Number.isFinite(Date.parse(latest.published_at))) throw new Error("invalid time metadata");
  if (snapshot.business_date !== latest.business_date || snapshot.published_at !== latest.published_at) throw new Error("time mismatch");
  if (version === 3 && (typeof latest.task_id !== "string" || !latest.task_id || !latest.platform || !snapshot.platform)) throw new Error("missing identity");
  if (platform === RankingPlatform.淘宝) {
    // 任务窗口来自采集器，不使用每页更新时间来伪造统一快照时刻。
    const start = latest.started_at ? Date.parse(latest.started_at) : NaN;
    const end = latest.finished_at ? Date.parse(latest.finished_at) : NaN;
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start || end > Date.parse(latest.published_at) || snapshot.started_at !== latest.started_at || snapshot.finished_at !== latest.finished_at) throw new Error("invalid collection window");
  }
  safePublicUrl(latest.data_url);
  safePublicUrl(latest.csv_url);
  // 复制规范化记录，避免修改远端对象或把未知指标补成零。
  const records = snapshot.records.map((row) => {
    if (!row || typeof row !== "object" || !Number.isInteger(row.rank) || row.rank < 1) throw new Error("invalid row");
    if ([row.category, row.level1, row.level2, row.level3, row.product_name, row.shop_name].some((value) => typeof value !== "string" || !value.trim())) throw new Error("invalid row text");
    if (row.platform !== undefined && row.platform !== platform) throw new Error("row platform mismatch");
    // 历史图片可缺失，保留现有占位行为。
    const thumbnail = row.thumbnail_url ?? "";
    if (typeof thumbnail !== "string") throw new Error("invalid thumbnail");
    if (thumbnail) safePublicUrl(thumbnail);
    if (platform === RankingPlatform.淘宝) {
      safePublicUrl(row.product_url);
      if (row.newly_on_ranking !== null || row.pay_amount !== undefined || row.pay_combo_count !== undefined) throw new Error("foreign metrics");
      for (const [raw, bound] of [[row.pay_buyer_count, row.pay_buyer_count_min_value], [row.visitor_count, row.visitor_count_min_value]]) {
        if (raw !== null && typeof raw !== "string") throw new Error("invalid raw metric");
        if (bound !== null && (typeof bound !== "string" || !/^\d+(?:\.\d+)?$/.test(bound) || !Number.isFinite(Number(bound)))) throw new Error("invalid metric bound");
        if ((raw === null || raw === "-") !== (bound === null)) throw new Error("missing metric mismatch");
      }
    } else if (typeof row.pay_amount !== "string" || typeof row.pay_combo_count !== "string" || typeof row.newly_on_ranking !== "boolean") throw new Error("missing Compass metrics");
    // 淘宝接口名称包含HTML实体；只解码一次供文本展示和搜索，源快照与CSV仍保留原文。
    // React按文本渲染解码后的名称，不把名称作为HTML执行；旧抖音名称保持兼容。
    return { ...row, platform, thumbnail_url: thumbnail,
      product_name: platform === RankingPlatform.淘宝 ? decodeHTMLStrict(row.product_name) : row.product_name,
      shop_name: platform === RankingPlatform.淘宝 ? decodeHTMLStrict(row.shop_name) : row.shop_name,
    } as RankingRecord;
  });
  if (!Number.isInteger(latest.item_count) || latest.item_count !== records.length) throw new Error("item count mismatch");
  return { index: latest, records };
}

/** 每次请求绑定取消信号，迟到响应在解析和提交前都必须重新检查取消状态。 */
export async function loadRankingData(url: string, platform: RankingPlatform, signal: AbortSignal) {
  safePublicUrl(url);
  // fetch 即使被测试服务或浏览器缓存延迟，后续边界仍拒绝已经取消的任务。
  const indexResponse = await fetch(url, { cache: "no-store", signal });
  signal.throwIfAborted();
  if (!indexResponse.ok) throw new Error("latest unavailable");
  const latest = await indexResponse.json() as LatestIndex;
  signal.throwIfAborted();
  const dataResponse = await fetch(safePublicUrl(latest.data_url), { cache: "no-store", signal });
  signal.throwIfAborted();
  if (!dataResponse.ok) throw new Error("snapshot unavailable");
  const snapshot = await dataResponse.json() as DataSnapshot;
  signal.throwIfAborted();
  return decodeRankingBundle(latest, snapshot, platform);
}
