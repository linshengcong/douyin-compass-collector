/** 网页快照中的单条商品榜单记录，由采集端 WebPublisher 生成。 */
export interface RankingRecord {
  category: string;
  level1: string;
  level2: string;
  level3: string;
  rank: number;
  product_name: string;
  shop_name: string;
  pay_amount: string;
  pay_combo_count: string;
  newly_on_ranking: boolean;
}

/** latest.json 将网页定位到当前不可变快照和对应 CSV。 */
export interface LatestIndex {
  batch_id: string;
  business_date: string;
  published_at: string;
  successful_category_count: number;
  failed_category_count: number;
  item_count: number;
  data_url: string;
  csv_url: string;
}

/** gzip 快照经浏览器自动解压后暴露的公开数据形状。 */
export interface DataSnapshot {
  records: RankingRecord[];
}

/** 稳定的榜单排序字段，值与前端计算逻辑保持一致。 */
export enum SortField {
  排名 = "rank",
  用户支付金额 = "pay_amount",
  成交件数 = "pay_combo_count",
}

/** 稳定的榜单排序方向。 */
export enum SortDirection {
  升序 = "asc",
  降序 = "desc",
}

/** 数值筛选的比较方式，支持不限、单边阈值和双边区间。 */
export enum MetricOperator {
  不限 = "all",
  大于等于 = "minimum",
  小于等于 = "maximum",
  区间 = "range",
}

/** 页面筛选状态由桌面与移动视图共同使用。 */
export interface RankingFilters {
  keyword: string;
  level1: string;
  level2: string;
  level3: string;
  newOnly: boolean;
  payMinimum: string;
  payMaximum: string;
  payOperator: MetricOperator;
  countMinimum: string;
  countMaximum: string;
  countOperator: MetricOperator;
  sortField: SortField;
  sortDirection: SortDirection;
}
