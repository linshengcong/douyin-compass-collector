import test from "node:test";
import assert from "node:assert/strict";
import { MetricOperator, RankingPlatform, SortDirection, SortField } from "../src/types.ts";
import { defaultRankingFilters } from "../src/hooks/useRankingFilters.ts";
import { filterRankingRecords, metricLowerBound, rankingMetric, sortRankingRecords, platformSortFields } from "../src/lib/ranking.ts";
import { decodeRankingBundle, loadRankingData } from "../src/lib/data.ts";
import { bundle } from "./fixtures.mjs";

// 平台测试共享真实业务枚举，不复制前端的字符串代码。
const TAOBAO = RankingPlatform.淘宝;
const COMPASS = RankingPlatform.抖音;

test("平台默认值与可用排序字段保持独立", () => {
  assert.equal(defaultRankingFilters(TAOBAO).newOnly, false);
  assert.equal(defaultRankingFilters(COMPASS).newOnly, true);
  assert.deepEqual(platformSortFields(TAOBAO), [SortField.排名, SortField.支付买家数, SortField.访客数]);
  assert.deepEqual(platformSortFields(COMPASS), [SortField.排名, SortField.用户支付金额, SortField.成交件数]);
});

test("下界解析保留旧抖音数值，未知、空值与真实零分开", () => {
  assert.equal(metricLowerBound("¥1万-¥2.5万"), 10000);
  assert.equal(metricLowerBound("2.5万 ~ 5万"), 25000);
  assert.equal(metricLowerBound("0 ~ 10"), 0);
  for (const missing of [null, undefined, "", "-", "garbage", "5~junk"]) assert.equal(metricLowerBound(missing), null);
});

test("启用数值筛选后缺失排除，升降序中缺失始终最后", () => {
  // 合成数据覆盖空值、实际零和区间，不仅检查实现的普通路径。
  const rows = bundle(TAOBAO, 3).snapshot.records;
  rows[0].visitor_count = "0";
  rows[0].visitor_count_min_value = "0";
  rows[1].visitor_count = "1万 ~ 2万";
  rows[1].visitor_count_min_value = "10000";
  const filters = { ...defaultRankingFilters(TAOBAO), countOperator: MetricOperator.小于等于, countMaximum: "0" };
  assert.deepEqual(filterRankingRecords(rows, filters, TAOBAO).map((row) => row.rank), [1]);
  assert.equal(rankingMetric(rows[2], SortField.访客数), null);
  for (const direction of [SortDirection.升序, SortDirection.降序]) {
    assert.equal(sortRankingRecords(rows, { ...filters, sortField: SortField.访客数, sortDirection: direction }).at(-1).rank, 3);
  }
  assert.equal(filterRankingRecords(rows, defaultRankingFilters(TAOBAO), TAOBAO).length, 3);
});

test("新淘宝窗口、原始文本和旧抖音缺图片均可读取", () => {
  // 新格式必须具有窗口，旧抖音只补充明确已选平台和图片占位。
  const current = bundle(TAOBAO);
  assert.equal(decodeRankingBundle(current.latest, current.snapshot, TAOBAO).records[0].pay_buyer_count, "2.5万 ~ 5万");
  const legacy = bundle(COMPASS);
  for (const object of [legacy.latest, legacy.snapshot]) {
    delete object.platform; delete object.task_id; object.schema_version = 1;
  }
  delete legacy.snapshot.records[0].platform;
  delete legacy.snapshot.records[0].thumbnail_url;
  assert.equal(decodeRankingBundle(legacy.latest, legacy.snapshot, COMPASS).records[0].thumbnail_url, "");
});

test("淘宝名称解码一次HTML实体且源快照与旧抖音保持原文", () => {
  // 真实商品名含times/middot等实体；显示与搜索使用文本，不能把名称当HTML执行。
  const values = bundle(TAOBAO);
  values.snapshot.records[0].product_name = "观夏&times;Hirono &middot; &#x4E2D; &amp;lt;script&amp;gt;";
  values.snapshot.records[0].shop_name = "A&amp;B旗舰店";
  const row = decodeRankingBundle(values.latest, values.snapshot, TAOBAO).records[0];
  assert.equal(row.product_name, "观夏×Hirono · 中 &lt;script&gt;");
  assert.equal(row.shop_name, "A&B旗舰店");
  assert.equal(values.snapshot.records[0].shop_name, "A&amp;B旗舰店");
  const legacy = bundle(COMPASS);
  legacy.snapshot.records[0].product_name = "旧商品&times;";
  assert.equal(decodeRankingBundle(legacy.latest, legacy.snapshot, COMPASS).records[0].product_name, "旧商品&times;");
});

for (const mutation of ["platform", "task", "batch", "date", "window", "count", "version", "link", "metric"]) {
  test(`拒绝串用或损坏的快照：${mutation}`, () => {
    // 每个变体只改一个关联字段，证明加载入口确实校验身份。
    const values = bundle(TAOBAO);
    if (mutation === "platform") values.snapshot.platform = COMPASS;
    if (mutation === "task") values.snapshot.task_id = "different";
    if (mutation === "batch") values.snapshot.batch_id = "different";
    if (mutation === "date") values.snapshot.business_date = "2026-09-30";
    if (mutation === "window") values.snapshot.finished_at = "2026-10-01T15:00:00+08:00";
    if (mutation === "count") values.latest.item_count = 2;
    if (mutation === "version") values.snapshot.schema_version = 99;
    if (mutation === "link") values.snapshot.records[0].product_url = "javascript:alert(1)";
    if (mutation === "metric") values.snapshot.records[0].visitor_count_min_value = "0";
    assert.throws(() => decodeRankingBundle(values.latest, values.snapshot, TAOBAO));
  });
}

test("旧请求晚到时取消信号仍阻止结果进入页面", async () => {
  // fetch 假实现故意忽略 signal，验证解析边界而不只是 mock 自动取消。
  const originalFetch = globalThis.fetch;
  const values = bundle(COMPASS);
  let resolveSnapshot;
  globalThis.fetch = async (url) => ({ ok: true, json: url.endsWith("latest.json") ? async () => values.latest : () => new Promise((resolve) => { resolveSnapshot = resolve; }) });
  const controller = new AbortController();
  const pending = loadRankingData("https://example.invalid/compass/latest.json", COMPASS, controller.signal);
  try {
    // 等待确认旧请求已经在读取正文，而非靠固定时长猜测请求状态。
    while (!resolveSnapshot) await new Promise((resolve) => setImmediate(resolve));
    controller.abort();
    resolveSnapshot(values.snapshot);
    await assert.rejects(pending, { name: "AbortError" });
  } finally { globalThis.fetch = originalFetch; }
});
