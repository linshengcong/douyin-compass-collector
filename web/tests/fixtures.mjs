import { RankingPlatform } from "../src/types.ts";

/** 合成快照从不来自账号接口，只用于测试公开数据契约。 */
export function bundle(platform = RankingPlatform.淘宝, count = 1) {
  // 两平台可故意有相同批次 ID，平台和任务仍必须独立匹配。
  const metadata = { schema_version: 3, platform, task_id: `${platform}_task`, batch_id: "a".repeat(32), business_date: "2026-10-01", published_at: "2026-10-01T14:06:00+08:00", started_at: "2026-10-01T14:00:00+08:00", finished_at: "2026-10-01T14:05:00+08:00" };
  const records = Array.from({ length: count }, (_, index) => ({
    platform, category: "一级 > 二级 > 三级", level1: "一级", level2: "二级", level3: "三级", rank: index + 1,
    thumbnail_url: "", product_name: `${platform}合成商品${index + 1}`, shop_name: "合成店铺",
    ...(platform === RankingPlatform.淘宝 ? {
      product_url: "https://sycm.taobao.com/mc/common/tm_item_redirect.htm?mi_id=synthetic",
      pay_buyer_count: "2.5万 ~ 5万", pay_buyer_count_min_value: "25000.0", visitor_count: null, visitor_count_min_value: null, newly_on_ranking: null,
    } : { pay_amount: "¥1万-¥2.5万", pay_combo_count: "10-25", newly_on_ranking: true }),
  }));
  return { latest: { ...metadata, successful_category_count: 1, failed_category_count: 0, item_count: count, data_url: `https://example.invalid/${platform}/data.json`, csv_url: `https://example.invalid/${platform}/data.csv` }, snapshot: { ...metadata, records } };
}
