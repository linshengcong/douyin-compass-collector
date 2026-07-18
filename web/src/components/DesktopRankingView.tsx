import { RankMark } from "./RankMark";
import { MetricOperator, SortDirection, SortField, type RankingFilters, type RankingRecord } from "../types";

interface DesktopRankingViewProps {
  records: RankingRecord[];
  filters: RankingFilters;
  options: { level1: string[]; level2: string[]; level3: string[] };
  page: number;
  pageSize: number;
  totalPages: number;
  onSetFilters: (updater: (current: RankingFilters) => RankingFilters) => void;
  onSelectLevel1: (value: string) => void;
  onSelectLevel2: (value: string) => void;
  onSelectLevel3: (value: string) => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  onReset: () => void;
}

/** 保留桌面端表格和页码分页，避免移动端改版影响大屏工作流。 */
export function DesktopRankingView(props: DesktopRankingViewProps) {
  const { records, filters, options, page, pageSize, totalPages, onSetFilters, onSelectLevel1, onSelectLevel2, onSelectLevel3, onPageChange, onPageSizeChange, onReset } = props;
  const pageRecords = records.slice((page - 1) * pageSize, page * pageSize);
  const pages = totalPages <= 7 ? Array.from({ length: totalPages }, (_, index) => index + 1) : [1, Math.max(2, page - 1), page, Math.min(totalPages - 1, page + 1), totalPages].filter((item, index, values) => values.indexOf(item) === index);

  return <div className="desktop-view">
    <section className="filter-card" aria-label="榜单筛选">
      <div className="search-row"><label className="search-field"><span>⌕</span><input value={filters.keyword} onChange={(event) => onSetFilters((current) => ({ ...current, keyword: event.target.value }))} placeholder="搜索商品名称或店铺名" /></label><label className="check-field check-field-prominent"><input type="checkbox" checked={filters.newOnly} onChange={(event) => onSetFilters((current) => ({ ...current, newOnly: event.target.checked }))} /><span>仅看首次上榜</span></label></div>
      <div className="filter-grid">
        <DesktopSelect label="一级类目" value={filters.level1} options={options.level1} onChange={onSelectLevel1} />
        <DesktopSelect label="二级类目" value={filters.level2} options={options.level2} onChange={onSelectLevel2} />
        <DesktopSelect label="三级类目" value={filters.level3} options={options.level3} onChange={onSelectLevel3} />
        <DesktopThreshold label="支付金额" unit="元" value={filters.payMinimum} onChange={(value) => onSetFilters((current) => ({ ...current, payMinimum: value, payMaximum: "", payOperator: value ? MetricOperator.大于等于 : MetricOperator.不限 }))} />
        <DesktopThreshold label="成交件数" unit="件" value={filters.countMinimum} onChange={(value) => onSetFilters((current) => ({ ...current, countMinimum: value, countMaximum: "", countOperator: value ? MetricOperator.大于等于 : MetricOperator.不限 }))} />
      </div>
      <div className="toolbar"><span>排序</span><select value={filters.sortField} onChange={(event) => onSetFilters((current) => ({ ...current, sortField: event.target.value as SortField }))}>{Object.values(SortField).map((item) => <option value={item} key={item}>{item}</option>)}</select><button type="button" className="sort-button" onClick={() => onSetFilters((current) => ({ ...current, sortDirection: current.sortDirection === SortDirection.升序 ? SortDirection.降序 : SortDirection.升序 }))}>{filters.sortDirection === SortDirection.升序 ? "升序 ↑" : "降序 ↓"}</button><button type="button" className="reset-button" onClick={onReset}>↻ 重置筛选</button></div>
    </section>
    <section className="result-card"><div className="result-heading"><strong>♛ 共 {records.length.toLocaleString()} 条结果</strong></div>{pageRecords.length ? <div className="desktop-table"><table><thead><tr><th>排名</th><th>分类</th><th>商品</th><th>店铺名称</th><th>用户支付金额</th><th>成交件数</th><th>首次上榜</th></tr></thead><tbody>{pageRecords.map((item, index) => <tr key={`${item.category}-${item.rank}-${item.product_name}`}><td><RankMark rank={(page - 1) * pageSize + index + 1} /></td><td className="category-cell">{item.category}</td><td><b>{item.product_name}</b></td><td>{item.shop_name}</td><td>{item.pay_amount}</td><td>{item.pay_combo_count}</td><td>{item.newly_on_ranking ? <em>首次上榜</em> : "−"}</td></tr>)}</tbody></table></div> : <div className="empty-state">没有符合当前筛选条件的商品</div>}<nav className="pagination"><span className="total-count">共 {records.length.toLocaleString()} 条</span><div className="page-actions"><button type="button" disabled={page === 1} onClick={() => onPageChange(page - 1)}>‹</button>{pages.map((item) => <button type="button" className={item === page ? "active-page" : ""} key={item} onClick={() => onPageChange(item)}>{item}</button>)}<button type="button" disabled={page === totalPages} onClick={() => onPageChange(page + 1)}>›</button></div><label className="page-size"><select value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>{[10, 20, 50].map((item) => <option key={item} value={item}>{item} 条 / 页</option>)}</select></label></nav></section>
  </div>;
}

/** 桌面端复用的原生下拉筛选字段。 */
function DesktopSelect({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (value: string) => void }) { return <label className="filter-field"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((item) => <option key={item}>{item}</option>)}</select></label>; }

/** 桌面端保留可直接输入的数值阈值语义。 */
function DesktopThreshold({ label, unit, value, onChange }: { label: string; unit: string; value: string; onChange: (value: string) => void }) { return <label className="filter-field"><span>{label}</span><div className="numeric-input"><input inputMode="decimal" value={value} onChange={(event) => onChange(event.target.value.replace(/[^\d.]/g, ""))} placeholder="不限" /><small>{unit}</small></div></label>; }
