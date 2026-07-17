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

// latest.json 是不可缓存索引，负责指向当前版本化数据文件和 CSV。
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

// gzip 数据文件中保存本次完整榜单，浏览器通过 HTTP Content-Encoding 自动解压。
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

const PAGE_SIZE = 50;
const dataIndexUrl = import.meta.env.VITE_DATA_INDEX_URL as string | undefined;

/** Convert Chinese compact display ranges into a comparable lower bound. */
function metricLowerBound(value: string): number {
  const matched = value.replaceAll("¥", "").match(/([\d.]+)(亿|万)?/);
  if (!matched) return 0;
  const multiplier = matched[2] === "亿" ? 100_000_000 : matched[2] === "万" ? 10_000 : 1;
  return Number(matched[1]) * multiplier;
}

/** Render one responsive ranking table backed only by the current public snapshot. */
function RankingApp() {
  // records、index 和 loadError 共同表示远端数据的加载状态。
  const [records, setRecords] = useState<RankingRecord[]>([]);
  const [index, setIndex] = useState<LatestIndex | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 筛选状态局限在页面，刷新页面会重新读取最新公开快照。
  const [keyword, setKeyword] = useState("");
  const [level1, setLevel1] = useState("全部");
  const [level2, setLevel2] = useState("全部");
  const [level3, setLevel3] = useState("全部");
  const [newOnly, setNewOnly] = useState(false);
  const [payMinimum, setPayMinimum] = useState("");
  const [countMinimum, setCountMinimum] = useState("");
  const [sortField, setSortField] = useState<SortField>(SortField.排名);
  const [sortDirection, setSortDirection] = useState<SortDirection>(SortDirection.升序);
  const [page, setPage] = useState(1);

  useEffect(() => {
    // 先获取 latest 索引，再获取其不可变数据文件；两个失败点分别呈现同一安全错误状态。
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
      const leftValue = sortField === SortField.排名 ? left.rank : metricLowerBound(sortField === SortField.用户支付金额 ? left.pay_amount : left.pay_combo_count);
      const rightValue = sortField === SortField.排名 ? right.rank : metricLowerBound(sortField === SortField.用户支付金额 ? right.pay_amount : right.pay_combo_count);
      return sortDirection === SortDirection.升序 ? leftValue - rightValue : rightValue - leftValue;
    });
  }, [countMinimum, keyword, level1, level2, level3, newOnly, payMinimum, records, sortDirection, sortField]);

  const totalPages = Math.max(1, Math.ceil(filteredRecords.length / PAGE_SIZE));
  const pageRecords = filteredRecords.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  useEffect(() => {
    // 任意筛选变化都回到第一页，避免保留超出新结果范围的页码。
    setPage(1);
  }, [keyword, level1, level2, level3, newOnly, payMinimum, countMinimum, sortField, sortDirection]);

  const selectChange = (
    setter: (value: string) => void,
    value: string,
    resets?: Array<(value: string) => void>,
  ) => {
    setter(value);
    resets?.forEach((reset) => reset("全部"));
  };

  if (loading) return <main className="state-card">正在加载最新榜单…</main>;
  if (loadError) return <main className="state-card error-state">{loadError}</main>;

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">DOUYIN COMPASS</p>
          <h1>商品实时榜</h1>
          <p className="subtitle">按实时榜单筛选、比较和浏览商品表现</p>
        </div>
        <div className="snapshot-meta">
          <span>数据日期 {index?.business_date}</span>
          <strong>{index?.item_count.toLocaleString()} 条商品</strong>
          {index?.csv_url ? <a href={index.csv_url}>下载 CSV</a> : null}
        </div>
      </header>

      <section className="filter-card" aria-label="榜单筛选">
        <label className="search-field">
          <span>⌕</span>
          <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索商品名或店铺名" />
        </label>
        <div className="filter-grid">
          <FilterSelect label="一级类目" value={level1} options={level1Options} onChange={(value) => selectChange(setLevel1, value, [setLevel2, setLevel3])} />
          <FilterSelect label="二级类目" value={level2} options={level2Options} onChange={(value) => selectChange(setLevel2, value, [setLevel3])} />
          <FilterSelect label="三级类目" value={level3} options={level3Options} onChange={(value) => setLevel3(value)} />
          <label><span>支付金额 ≥</span><input inputMode="decimal" value={payMinimum} onChange={(event) => setPayMinimum(event.target.value)} placeholder="元" /></label>
          <label><span>成交件数 ≥</span><input inputMode="decimal" value={countMinimum} onChange={(event) => setCountMinimum(event.target.value)} placeholder="件" /></label>
          <label className="toggle-field"><input type="checkbox" checked={newOnly} onChange={(event) => setNewOnly(event.target.checked)} />仅看首次上榜</label>
        </div>
        <div className="toolbar">
          <span>排序</span>
          <select value={sortField} onChange={(event) => setSortField(event.target.value as SortField)}>{Object.values(SortField).map((item) => <option key={item}>{item}</option>)}</select>
          <button className="sort-button" onClick={() => setSortDirection(sortDirection === SortDirection.升序 ? SortDirection.降序 : SortDirection.升序)}>{sortDirection === SortDirection.升序 ? "升序 ↑" : "降序 ↓"}</button>
          <button className="reset-button" onClick={() => { setKeyword(""); setLevel1("全部"); setLevel2("全部"); setLevel3("全部"); setNewOnly(false); setPayMinimum(""); setCountMinimum(""); setSortField(SortField.排名); setSortDirection(SortDirection.升序); }}>重置筛选</button>
        </div>
      </section>

      <section className="result-card">
        <div className="notice">ⓘ 榜单数据来自本次已完成采集；跳过的异常分类不会进入当前展示。</div>
        <div className="result-heading"><strong>共 {filteredRecords.length.toLocaleString()} 条结果</strong><span>成功分类 {index?.successful_category_count} · 跳过分类 {index?.failed_category_count}</span></div>
        {pageRecords.length === 0 ? <div className="empty-state">没有符合当前筛选条件的商品</div> : <><div className="desktop-table"><table><thead><tr><th>排名</th><th>分类</th><th>商品</th><th>店铺名称</th><th>用户支付金额</th><th>成交件数</th><th>首次上榜</th></tr></thead><tbody>{pageRecords.map((item) => <tr key={`${item.category}-${item.rank}-${item.product_name}`}><td><b>TOP {item.rank}</b></td><td>{item.category}</td><td>{item.product_name}</td><td>{item.shop_name}</td><td>{item.pay_amount}</td><td>{item.pay_combo_count}</td><td>{item.newly_on_ranking ? <em>首次上榜</em> : "-"}</td></tr>)}</tbody></table></div><div className="mobile-list">{pageRecords.map((item) => <article key={`${item.category}-${item.rank}-${item.product_name}`}><div className="rank-pill">TOP {item.rank}</div><div><p className="category-line">{item.category}</p><h2>{item.product_name}</h2><p>{item.shop_name}</p><div className="metric-row"><span>支付 {item.pay_amount}</span><span>成交 {item.pay_combo_count}</span>{item.newly_on_ranking ? <em>首次上榜</em> : null}</div></div></article>)}</div></>}
        <nav className="pagination" aria-label="分页"><button disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button><span>{page} / {totalPages}</span><button disabled={page === totalPages} onClick={() => setPage(page + 1)}>下一页</button></nav>
      </section>
    </main>
  );
}

/** Render one labelled controlled select so category filters share identical interaction. */
function FilterSelect({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (value: string) => void }) {
  return <label><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((item) => <option key={item}>{item}</option>)}</select></label>;
}

createRoot(document.getElementById("root")!).render(<RankingApp />);
