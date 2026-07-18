import { useEffect, useRef, useState } from "react";
import { DEFAULT_RANKING_FILTERS } from "../hooks/useRankingFilters";
import { useInfiniteReveal } from "../hooks/useInfiniteReveal";
import { SortDirection, SortField, type LatestIndex, type RankingFilters, type RankingRecord } from "../types";
import { CompositeFilterSheet } from "./CompositeFilterSheet";
import { RankMark } from "./RankMark";

interface MobileRankingViewProps {
  snapshotRecords: RankingRecord[];
  records: RankingRecord[];
  index: LatestIndex | null;
  filters: RankingFilters;
  onSetFilters: (updater: (current: RankingFilters) => RankingFilters) => void;
}

/** 渲染移动端独立榜单：突出首次上榜、底部筛选与每批 50 条上拉加载。 */
export function MobileRankingView(props: MobileRankingViewProps) {
  const { snapshotRecords, records, index, filters, onSetFilters } = props;
  // 综合筛选弹层只在用户主动点击筛选时出现，底层列表不会因草稿变化而更新。
  const [isCompositeFilterOpen, setCompositeFilterOpen] = useState(false);
  // 仅在工具栏触顶后启用背景层，避免页面中段出现突兀的色块。
  const [isToolbarStuck, setToolbarStuck] = useState(false);
  // 仅在已应用条件发生变化时显示短暂加载态，明确列表正在刷新。
  const [isFiltering, setFiltering] = useState(false);
  // 工具栏节点用于根据实际页面位置判断吸顶状态。
  const toolbarRef = useRef<HTMLElement | null>(null);
  const { visibleItems, visibleCount, sentinelRef } = useInfiniteReveal(records, 50);

  useEffect(() => {
    /** 根据工具栏的实际顶部位置同步吸顶视觉状态。 */
    const updateToolbarState = () => setToolbarStuck((toolbarRef.current?.getBoundingClientRect().top ?? 1) <= 0);
    updateToolbarState();
    window.addEventListener("scroll", updateToolbarState, { passive: true });
    window.addEventListener("resize", updateToolbarState);
    return () => {
      window.removeEventListener("scroll", updateToolbarState);
      window.removeEventListener("resize", updateToolbarState);
    };
  }, []);
  // 顶部统计始终使用完整快照，不受榜单 Tab、关键词或类目筛选影响。
  const newlyOnRankingCount = snapshotRecords.filter((item) => item.newly_on_ranking).length;
  const activeTab = filters.newOnly ? "new" : filters.sortField === SortField.用户支付金额 && filters.sortDirection === SortDirection.降序 ? "pay" : "all";
  /** 将顶部榜单入口映射为现有共享筛选与排序状态。 */
  const selectTab = (tab: "all" | "new" | "pay") => {
    onSetFilters((current) => {
      if (tab === "new") return { ...current, newOnly: true, sortField: SortField.排名, sortDirection: SortDirection.升序 };
      if (tab === "pay") return { ...current, newOnly: false, sortField: SortField.用户支付金额, sortDirection: SortDirection.降序 };
      return { ...current, newOnly: false, sortField: SortField.排名, sortDirection: SortDirection.升序 };
    });
  };
  /** 提交发生变化的移动端筛选，展示反馈后将页面平滑带回榜单顶部。 */
  const applyMobileFilters = (nextFilters: RankingFilters) => {
    const hasChanged = JSON.stringify(filters) !== JSON.stringify(nextFilters);
    setCompositeFilterOpen(false);
    if (!hasChanged) return;
    onSetFilters(() => nextFilters);
    setFiltering(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
    window.setTimeout(() => setFiltering(false), 360);
  };
  /** 将顶部发布时间转为用户要求的分钟精度。 */
  const publishedMinute = formatPublishedMinute(index?.published_at);

  return <div className="mobile-view">
    <header className="mobile-hero">
      <div><h1>商品实时榜</h1><span>数据更新：{publishedMinute}</span></div>
      {index?.csv_url ? <a className="mobile-csv" href={index.csv_url}>⇩ 表格下载</a> : null}
    </header>
    <section ref={toolbarRef} className={`mobile-sticky-toolbar${isToolbarStuck ? " is-stuck" : ""}`} aria-label="榜单筛选">
      <button type="button" className="sticky-search" onClick={() => setCompositeFilterOpen(true)}><span>⌕</span><span className="sticky-search-placeholder">搜索商品名称或店铺名</span></button>
      <button type="button" className="sticky-filter-button" onClick={() => setCompositeFilterOpen(true)}>筛选 <b>⌘</b></button>
    </section>
    <p className="mobile-notice">ⓘ 仅展示已完成采集的商品；筛选条件在点击“应用筛选”后生效</p>
    <section className="mobile-results" aria-busy={isFiltering}>
      <div className="mobile-result-title"><strong>共 {records.length.toLocaleString()} 条结果</strong><span>已显示 {visibleCount} 条</span></div>
      {isFiltering ? <div className="mobile-rendering"><i />筛选结果加载中…</div> : null}
      {visibleItems.length ? <div className="mobile-list">{visibleItems.map((item, index) => <article key={`${item.category}-${item.rank}-${item.product_name}`}><RankMark rank={index + 1} /><div className="mobile-card-body"><p className="category-line">{item.category}</p><h2>{item.product_name}</h2><p className="shop-line">{item.shop_name}</p><div className="metric-row"><span><small>用户支付金额</small>{item.pay_amount}</span><span><small>成交件数</small>{item.pay_combo_count}</span>{item.newly_on_ranking ? <em>首次上榜</em> : null}</div></div></article>)}</div> : <div className="empty-state">没有符合当前筛选条件的商品</div>}
      {visibleCount < records.length ? <div className="mobile-load-more" ref={sentinelRef}><i /><span>上拉加载更多</span></div> : records.length > 0 ? <div className="mobile-load-more finished">已加载全部 {records.length.toLocaleString()} 条</div> : null}
    </section>
    {isCompositeFilterOpen ? <CompositeFilterSheet records={snapshotRecords} filters={filters} onClose={() => setCompositeFilterOpen(false)} onApply={applyMobileFilters} onReset={() => applyMobileFilters(DEFAULT_RANKING_FILTERS)} /> : null}
  </div>;
}

/** 将 ISO 发布时间显示为分钟精度，缺失或非法时提供稳定占位。 */
function formatPublishedMinute(value: string | undefined): string {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "--";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(parsed).replaceAll("/", "-");
}
