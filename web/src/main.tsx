import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

// 网站数据格式由 Python WebPublisher 生成，字段只对应现有 CSV 的七列。
interface RankingRecord {
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

// latest.json 负责将网页定位到当前不可变数据快照与下载 CSV。
interface LatestIndex {
  batch_id: string;
  business_date: string;
  published_at: string;
  successful_category_count: number;
  failed_category_count: number;
  item_count: number;
  data_url: string;
  csv_url: string;
}

// gzip 数据文件经 HTTP Content-Encoding 自动解压后返回完整榜单。
interface DataSnapshot {
  records: RankingRecord[];
}

enum SortField {
  排名 = "rank",
  用户支付金额 = "pay_amount",
  成交件数 = "pay_combo_count",
}

enum SortDirection {
  升序 = "asc",
  降序 = "desc",
}

// 每页可选数量与设计稿一致，并在本地分页中即时生效。
const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;
const dataIndexUrl = import.meta.env.VITE_DATA_INDEX_URL as string | undefined;

/** 将中文金额或件数区间转换为可比较的最低数值。 */
function metricLowerBound(value: string): number {
  const matched = value.replaceAll("¥", "").match(/([\d.]+)(亿|万)?/);
  if (!matched) return 0;
  const multiplier = matched[2] === "亿" ? 100_000_000 : matched[2] === "万" ? 10_000 : 1;
  return Number(matched[1]) * multiplier;
}

/** 返回紧凑分页条需要展示的页码，防止数千页同时渲染。 */
function visiblePages(page: number, totalPages: number): Array<number | "ellipsis"> {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1);
  const middle = [page - 1, page, page + 1].filter((item) => item > 1 && item < totalPages);
  return [1, ...(page > 3 ? ["ellipsis" as const] : []), ...middle, ...(page < totalPages - 2 ? ["ellipsis" as const] : []), totalPages];
}

/** 按实时榜单筛选、排序并以响应式表格展示当前公开快照。 */
function RankingApp() {
  // records、index 与 loadError 共同表示远端快照的读取状态。
  const [records, setRecords] = useState<RankingRecord[]>([]);
  const [index, setIndex] = useState<LatestIndex | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 所有筛选状态只保存在浏览器内，刷新后会重新读取最新榜单。
  const [keyword, setKeyword] = useState("");
  const [level1, setLevel1] = useState("全部");
  const [level2, setLevel2] = useState("全部");
  const [level3, setLevel3] = useState("全部");
  // 首次上榜是当前榜单的默认关注重点，用户可随时取消勾选查看全量结果。
  const [newOnly, setNewOnly] = useState(true);
  const [payMinimum, setPayMinimum] = useState("");
  const [countMinimum, setCountMinimum] = useState("");
  const [sortField, setSortField] = useState<SortField>(SortField.排名);
  const [sortDirection, setSortDirection] = useState<SortDirection>(SortDirection.升序);
  const [page, setPage] = useState(1);
  // pageSize 控制本地分页密度，不影响 OSS 数据请求量。
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZE_OPTIONS)[number]>(10);

  useEffect(() => {
    // 先读取 latest 索引，再读取其不可变数据快照；失败时统一呈现安全错误文案。
    if (!dataIndexUrl) {
      setLoadError("尚未配置网页数据地址");
      setLoading(false);
      return;
    }
    void (async () => {
      try {
        const indexResponse = await fetch(dataIndexUrl, { cache: "no-store" });
        if (!indexResponse.ok) throw new Error("latest index unavailable");
        const latest = (await indexResponse.json()) as LatestIndex;
        const dataResponse = await fetch(latest.data_url);
        if (!dataResponse.ok) throw new Error("snapshot unavailable");
        const snapshot = (await dataResponse.json()) as DataSnapshot;
        if (!Array.isArray(snapshot.records)) throw new Error("snapshot contract invalid");
        setIndex(latest);
        setRecords(snapshot.records);
      } catch {
        setLoadError("暂时无法读取最新榜单，请稍后刷新重试");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const level1Options = useMemo(
    () => ["全部", ...Array.from(new Set(records.map((item) => item.level1)))],
    [records],
  );
  const level2Options = useMemo(() => {
    const scoped = level1 === "全部" ? records : records.filter((item) => item.level1 === level1);
    return ["全部", ...Array.from(new Set(scoped.map((item) => item.level2)))];
  }, [level1, records]);
  const level3Options = useMemo(() => {
    const scoped = records.filter(
      (item) =>
        (level1 === "全部" || item.level1 === level1) &&
        (level2 === "全部" || item.level2 === level2),
    );
    return ["全部", ...Array.from(new Set(scoped.map((item) => item.level3)))];
  }, [level1, level2, records]);

  const filteredRecords = useMemo(() => {
    const normalizedKeyword = keyword.trim().toLocaleLowerCase();
    const minimumPay = Number(payMinimum) || 0;
    const minimumCount = Number(countMinimum) || 0;
    const filtered = records.filter((item) => {
      const hasKeyword =
        !normalizedKeyword ||
        item.product_name.toLocaleLowerCase().includes(normalizedKeyword) ||
        item.shop_name.toLocaleLowerCase().includes(normalizedKeyword);
      return (
        hasKeyword &&
        (level1 === "全部" || item.level1 === level1) &&
        (level2 === "全部" || item.level2 === level2) &&
        (level3 === "全部" || item.level3 === level3) &&
        (!newOnly || item.newly_on_ranking) &&
        metricLowerBound(item.pay_amount) >= minimumPay &&
        metricLowerBound(item.pay_combo_count) >= minimumCount
      );
    });
    return filtered.sort((left, right) => {
      const leftValue = sortField === SortField.排名
        ? left.rank
        : metricLowerBound(sortField === SortField.用户支付金额 ? left.pay_amount : left.pay_combo_count);
      const rightValue = sortField === SortField.排名
        ? right.rank
        : metricLowerBound(sortField === SortField.用户支付金额 ? right.pay_amount : right.pay_combo_count);
      return sortDirection === SortDirection.升序 ? leftValue - rightValue : rightValue - leftValue;
    });
  }, [countMinimum, keyword, level1, level2, level3, newOnly, payMinimum, records, sortDirection, sortField]);

  const totalPages = Math.max(1, Math.ceil(filteredRecords.length / pageSize));
  const pageRecords = filteredRecords.slice((page - 1) * pageSize, page * pageSize);

  useEffect(() => {
    // 任意筛选、排序或页容量变化均回到第一页，避免显示超出结果范围的空页。
    setPage(1);
  }, [keyword, level1, level2, level3, newOnly, payMinimum, countMinimum, sortField, sortDirection, pageSize]);

  /** 更新级联类目，并将其下级筛选恢复为“全部”。 */
  const selectChange = (
    setter: (value: string) => void,
    value: string,
    resets?: Array<(value: string) => void>,
  ) => {
    setter(value);
    resets?.forEach((reset) => reset("全部"));
  };

  /** 将筛选与排序恢复为初始状态，保留当前已加载的榜单数据。 */
  const resetFilters = () => {
    setKeyword("");
    setLevel1("全部");
    setLevel2("全部");
    setLevel3("全部");
    setNewOnly(true);
    setPayMinimum("");
    setCountMinimum("");
    setSortField(SortField.排名);
    setSortDirection(SortDirection.升序);
  };

  if (loading) return <main className="state-card">正在加载最新榜单…</main>;
  if (loadError) return <main className="state-card error-state">{loadError}</main>;

  return (
    <main className="page-shell">
      <header className="hero">
        <div className="brand-intro">
          <p className="eyebrow"><span className="brand-icon">✦</span> DOUYIN COMPASS</p>
          <h1>商品实时榜 <span aria-hidden="true">✧</span></h1>
          <p className="subtitle">按实时榜单筛选、比较和浏览商品表现</p>
        </div>
        <div className="snapshot-meta" aria-label="当前榜单元数据">
          <span>▣ 数据日期&nbsp;&nbsp;{index?.business_date}</span>
          <strong>{index?.item_count.toLocaleString()} 条商品</strong>
          {index?.csv_url ? <a href={index.csv_url}>⇩ 下载 CSV</a> : null}
        </div>
      </header>

      <section className="filter-card" aria-label="榜单筛选">
        <div className="search-row">
          <label className="search-field">
            <span aria-hidden="true">⌕</span>
            <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索商品名称或店铺名" />
          </label>
          <label className="check-field check-field-prominent"><input type="checkbox" checked={newOnly} onChange={(event) => setNewOnly(event.target.checked)} /><span>仅看首次上榜</span></label>
        </div>
        <div className="filter-grid">
          <FilterSelect label="一级类目" value={level1} options={level1Options} onChange={(value) => selectChange(setLevel1, value, [setLevel2, setLevel3])} />
          <FilterSelect label="二级类目" value={level2} options={level2Options} onChange={(value) => selectChange(setLevel2, value, [setLevel3])} />
          <FilterSelect label="三级类目" value={level3} options={level3Options} onChange={(value) => setLevel3(value)} />
          <NumericFilter label="支付金额" value={payMinimum} unit="元" onChange={setPayMinimum} />
          <NumericFilter label="成交件数" value={countMinimum} unit="件" onChange={setCountMinimum} />
        </div>
        <div className="toolbar">
          <span>排序</span>
          <select value={sortField} onChange={(event) => setSortField(event.target.value as SortField)}>{Object.values(SortField).map((item) => <option key={item} value={item}>{item}</option>)}</select>
          <button className="sort-button" onClick={() => setSortDirection(sortDirection === SortDirection.升序 ? SortDirection.降序 : SortDirection.升序)}>{sortDirection === SortDirection.升序 ? "升序 ↑" : "降序 ↓"}</button>
          <button className="reset-button" onClick={resetFilters}>↻&nbsp; 重置筛选</button>
        </div>
      </section>

      <p className="notice">ⓘ&nbsp;&nbsp;榜单数据来自本次已完成采集；跳过的异常分类不会进入当前展示。</p>

      <section className="result-card">
        <div className="result-heading">
          <strong>♛&nbsp;&nbsp;共 {filteredRecords.length.toLocaleString()} 条结果</strong>
          <span>成功分类 {index?.successful_category_count} · 跳过分类 {index?.failed_category_count}</span>
        </div>
        {pageRecords.length === 0 ? <div className="empty-state">没有符合当前筛选条件的商品</div> : <>
          <div className="desktop-table"><table><thead><tr><th>排名</th><th>分类</th><th>商品</th><th>店铺名称</th><th>用户支付金额</th><th>成交件数</th><th>首次上榜</th></tr></thead><tbody>{pageRecords.map((item, itemIndex) => <tr key={`${item.category}-${item.rank}-${item.product_name}`}><td><RankMark rank={(page - 1) * pageSize + itemIndex + 1} /></td><td className="category-cell">{item.category}</td><td><div className="product-cell"><b>{item.product_name}</b></div></td><td>{item.shop_name}</td><td>{item.pay_amount}</td><td>{item.pay_combo_count}</td><td>{item.newly_on_ranking ? <em>首次上榜</em> : "−"}</td></tr>)}</tbody></table></div>
          <div className="mobile-list">{pageRecords.map((item, itemIndex) => <article key={`${item.category}-${item.rank}-${item.product_name}`}><RankMark rank={(page - 1) * pageSize + itemIndex + 1} /><div><p className="category-line">{item.category}</p><h2>{item.product_name}</h2><p>{item.shop_name}</p><div className="metric-row"><span>支付 {item.pay_amount}</span><span>成交 {item.pay_combo_count}</span>{item.newly_on_ranking ? <em>首次上榜</em> : null}</div></div></article>)}</div>
        </>}
        <nav className="pagination" aria-label="分页">
          <span className="total-count">共 {filteredRecords.length.toLocaleString()} 条</span>
          <div className="page-actions"><button aria-label="上一页" disabled={page === 1} onClick={() => setPage(page - 1)}>‹</button>{visiblePages(page, totalPages).map((item, itemIndex) => item === "ellipsis" ? <span className="ellipsis" key={`ellipsis-${itemIndex}`}>…</span> : <button className={item === page ? "active-page" : ""} key={item} onClick={() => setPage(item)}>{item}</button>)}<button aria-label="下一页" disabled={page === totalPages} onClick={() => setPage(page + 1)}>›</button></div>
          <label className="page-size"><select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value) as (typeof PAGE_SIZE_OPTIONS)[number])}>{PAGE_SIZE_OPTIONS.map((item) => <option key={item} value={item}>{item} 条 / 页</option>)}</select></label>
        </nav>
      </section>
    </main>
  );
}

/** 渲染当前结果顺序的排名标识，前三名仅使用克制的数字强调。 */
function RankMark({ rank }: { rank: number }) {
  return <span className={`rank-mark rank-${Math.min(rank, 4)}`}>{rank <= 3 ? `${rank}` : rank}</span>;
}

/** 渲染可复用的受控类目选择器，保持三级筛选交互一致。 */
function FilterSelect({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (value: string) => void }) {
  return <label className="filter-field"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((item) => <option key={item}>{item}</option>)}</select></label>;
}

/** 渲染金额或件数的最低阈值输入，不改变已有的“≥”筛选语义。 */
function NumericFilter({ label, value, unit, onChange }: { label: string; value: string; unit: string; onChange: (value: string) => void }) {
  return <label className="filter-field"><span>{label} ⓘ</span><div className="numeric-input"><input inputMode="decimal" value={value} onChange={(event) => onChange(event.target.value)} placeholder={`≥ ${unit}`} /><small>{unit}</small></div></label>;
}

createRoot(document.getElementById("root")!).render(<RankingApp />);
