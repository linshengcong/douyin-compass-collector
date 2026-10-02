/** 公开快照与界面使用同一平台标识，不把淘宝映射成抖音指标。 */
export enum RankingPlatform {
  抖音 = "compass",
  淘宝 = "taobao",
}

/** 网页快照中的单条商品榜单记录，由采集端 WebPublisher 生成。 */
export interface RankingRecord {
  /** 旧抖音快照在读取边界补充平台，列表内部始终明确归属。 */
  platform: RankingPlatform;
  category: string;
  level1: string;
  level2: string;
  level3: string;
  rank: number;
  /** 商品接口提供的公开缩略图地址；历史快照可能为空。 */
  thumbnail_url: string;
  product_name: string;
  shop_name: string;
  /** 抖音专属展示指标，淘宝记录没有这两个字段。 */
  pay_amount?: string;
  pay_combo_count?: string;
  /** 淘宝没有首次上榜状态，不能当成 false。 */
  newly_on_ranking: boolean | null;
  /** 淘宝商品接口提供的跳转链接，禁止按商品 ID 拼接。 */
  product_url?: string;
  /** 淘宝原始区间文本与实际人数下界，缺失保持 null。 */
  pay_buyer_count?: string | null;
  visitor_count?: string | null;
  pay_buyer_count_min_value?: string | null;
  visitor_count_min_value?: string | null;
}

/** latest.json 将网页定位到当前不可变快照和对应 CSV。 */
export interface LatestIndex {
  /** 历史抖音元信息可能没有平台或任务字段，新快照必须完整匹配。 */
  schema_version?: number;
  platform?: RankingPlatform;
  task_id?: string;
  /** 采集窗口独立于发布时刻，不强求每页更新时间一致。 */
  started_at?: string | null;
  finished_at?: string | null;
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
  /** 快照身份必须与 latest 索引一致，避免读取另一平台的文件。 */
  schema_version?: number;
  platform?: RankingPlatform;
  task_id?: string;
  batch_id: string;
  business_date: string;
  published_at: string;
  /** 新快照的采集窗口必须与索引相同。 */
  started_at?: string | null;
  finished_at?: string | null;
  records: RankingRecord[];
}

/** 稳定的榜单排序字段，值与前端计算逻辑保持一致。 */
export enum SortField {
  排名 = "rank",
  用户支付金额 = "pay_amount",
  成交件数 = "pay_combo_count",
  支付买家数 = "pay_buyer_count",
  访客数 = "visitor_count",
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
