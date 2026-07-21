import { useEffect, useRef, useState, type RefObject } from "react";
import { RankMark } from "./RankMark";
import { ImagePreview, ProductThumbnail } from "./ProductThumbnail";
import { MetricOperator, SortDirection, SortField, type RankingFilters, type RankingRecord } from "../types";
import { TruncatedTooltip } from "./TruncatedTooltip";

interface DesktopRankingViewProps {
  records: RankingRecord[];
  /** OSS latest 索引的发布时间，用于让桌面端明确展示当前榜单的新鲜度。 */
  publishedAt?: string;
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
  const { records, publishedAt, filters, options, page, pageSize, totalPages, onSetFilters, onSelectLevel1, onSelectLevel2, onSelectLevel3, onPageChange, onPageSizeChange, onReset } = props;
  const pageRecords = records.slice((page - 1) * pageSize, page * pageSize);
  const pages = totalPages <= 7 ? Array.from({ length: totalPages }, (_, index) => index + 1) : [1, Math.max(2, page - 1), page, Math.min(totalPages - 1, page + 1), totalPages].filter((item, index, values) => values.indexOf(item) === index);
  const formattedPublishedAt = formatPublishedAt(publishedAt);
  // 当前桌面列表仅保留一个选中图片，避免每行分别维护预览状态。
  const [preview, setPreview] = useState<{ imageUrl: string | null; productName: string }>({ imageUrl: null, productName: "" });

  return <div className="desktop-view">
    <section className="filter-card" aria-label="榜单筛选">
      <div className="search-row"><label className="search-field"><span>⌕</span><input value={filters.keyword} onChange={(event) => onSetFilters((current) => ({ ...current, keyword: event.target.value }))} placeholder="搜索商品名称或店铺名" /></label><label className="check-field check-field-prominent"><input type="checkbox" checked={filters.newOnly} onChange={(event) => onSetFilters((current) => ({ ...current, newOnly: event.target.checked }))} /><span>仅首次上榜</span></label></div>
      <div className="filter-grid">
        <DesktopChoiceMenu label="一级类目" value={filters.level1} options={options.level1} onChange={onSelectLevel1} />
        <DesktopChoiceMenu label="二级类目" value={filters.level2} options={options.level2} onChange={onSelectLevel2} />
        <DesktopChoiceMenu label="三级类目" value={filters.level3} options={options.level3} onChange={onSelectLevel3} />
        <DesktopMetricMenu label="支付金额" unit="元" quickValues={["10000", "50000", "100000"]} operator={filters.payOperator} minimum={filters.payMinimum} maximum={filters.payMaximum} onChange={(value) => onSetFilters((current) => ({ ...current, payOperator: value.operator, payMinimum: value.minimum, payMaximum: value.maximum }))} />
        <DesktopMetricMenu label="成交件数" unit="件" quickValues={["10", "50", "100"]} operator={filters.countOperator} minimum={filters.countMinimum} maximum={filters.countMaximum} onChange={(value) => onSetFilters((current) => ({ ...current, countOperator: value.operator, countMinimum: value.minimum, countMaximum: value.maximum }))} />
      </div>
      <div className="toolbar"><DesktopChoiceMenu label="排序" value={filters.sortField} options={Object.values(SortField)} onChange={(value) => onSetFilters((current) => ({ ...current, sortField: value as SortField }))} compact /><button type="button" className="sort-button" onClick={() => onSetFilters((current) => ({ ...current, sortDirection: current.sortDirection === SortDirection.升序 ? SortDirection.降序 : SortDirection.升序 }))}>{filters.sortDirection === SortDirection.升序 ? "↑ 升序" : "↓ 降序"}</button><button type="button" className="reset-button" onClick={onReset}>↻ 重置筛选</button></div>
    </section>
    <section className="result-card"><div className="result-heading"><strong>♛ 共 {records.length.toLocaleString()} 条结果</strong>{formattedPublishedAt && <time className="published-at" dateTime={publishedAt}>最新更新：{formattedPublishedAt}</time>}</div>{pageRecords.length ? <div className="desktop-table"><table><thead><tr><th>排名</th><th>分类</th><th>商品</th><th>店铺名称</th><th>用户支付金额</th><th>成交件数</th><th>首次上榜</th></tr></thead><tbody>{pageRecords.map((item, index) => <tr key={`${item.category}-${item.rank}-${item.product_name}`}><td><RankMark rank={(page - 1) * pageSize + index + 1} /></td><td className="category-cell"><TruncatedTooltip text={item.category} /></td><td><div className="desktop-product"><ProductThumbnail imageUrl={item.thumbnail_url} productName={item.product_name} size="desktop" onPreview={(imageUrl, productName) => setPreview({ imageUrl, productName })} /><TruncatedTooltip text={item.product_name} className="desktop-product-name" /></div></td><td>{item.shop_name}</td><td>{item.pay_amount}</td><td>{item.pay_combo_count}</td><td>{item.newly_on_ranking ? <em>首次上榜</em> : "−"}</td></tr>)}</tbody></table></div> : <div className="empty-state">没有符合当前筛选条件的商品</div>}<nav className="pagination"><span className="total-count">共 {records.length.toLocaleString()} 条</span><div className="page-actions"><button type="button" disabled={page === 1} onClick={() => onPageChange(page - 1)}>‹</button>{pages.map((item) => <button type="button" className={item === page ? "active-page" : ""} key={item} onClick={() => onPageChange(item)}>{item}</button>)}<button type="button" disabled={page === totalPages} onClick={() => onPageChange(page + 1)}>›</button></div><label className="page-size"><select value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>{[10, 20, 50].map((item) => <option key={item} value={item}>{item} 条 / 页</option>)}</select></label></nav></section><ImagePreview imageUrl={preview.imageUrl} productName={preview.productName} onClose={() => setPreview({ imageUrl: null, productName: "" })} />
  </div>;
}

/** 将公开快照时间按访问者本地时区格式化到分钟，异常值不在页面显示。 */
function formatPublishedAt(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (number: number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

interface DesktopChoiceMenuProps {
  /** 桌面浮层触发器上方显示的业务字段名称。 */
  label: string;
  /** 当前已应用的中文选项。 */
  value: string;
  /** 当前字段可选的中文候选项。 */
  options: string[];
  /** 选择新候选项后立即同步到共享筛选状态。 */
  onChange: (value: string) => void;
  /** 排序栏使用更紧凑的一行展示。 */
  compact?: boolean;
}

/** 显示桌面专用的紧凑选项浮层，不复用移动端底部抽屉。 */
function DesktopChoiceMenu({ label, value, options, onChange, compact = false }: DesktopChoiceMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useDesktopMenuDismiss(menuRef, open, () => setOpen(false));

  return <div className={`desktop-menu${compact ? " compact" : ""}`} ref={menuRef}><span className="desktop-menu-label">{label}</span><button type="button" className="desktop-menu-trigger" aria-expanded={open} onClick={() => setOpen((current) => !current)}><span>{desktopMenuOptionLabel(label, value)}</span><b>⌄</b></button>{open ? <div className="desktop-menu-popover" role="listbox" aria-label={`选择${label}`}>{options.map((option) => <button type="button" role="option" aria-selected={option === value} className={option === value ? "selected" : ""} key={option} onClick={() => { onChange(option); setOpen(false); }}><span>{desktopMenuOptionLabel(label, option)}</span>{option === value ? <b>✓</b> : null}</button>)}</div> : null}</div>;
}

interface DesktopMetricMenuProps {
  /** 桌面指标浮层对应的金额或件数名称。 */
  label: string;
  /** 指标数值在界面中显示的单位。 */
  unit: string;
  /** 与 H5 一致的常用快速阈值。 */
  quickValues: string[];
  /** 已应用的数值比较方式。 */
  operator: MetricOperator;
  /** 已应用的指标最小值。 */
  minimum: string;
  /** 已应用的指标最大值。 */
  maximum: string;
  /** 用户修改条件时立即同步到共享筛选状态。 */
  onChange: (value: { operator: MetricOperator; minimum: string; maximum: string }) => void;
}

/** 在桌面浮层内提供与 H5 等价的指标比较、输入和快捷阈值。 */
function DesktopMetricMenu({ label, unit, quickValues, operator, minimum, maximum, onChange }: DesktopMetricMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useDesktopMenuDismiss(menuRef, open, () => setOpen(false));
  const update = (next: Partial<{ operator: MetricOperator; minimum: string; maximum: string }>) => onChange({ operator: next.operator ?? operator, minimum: next.minimum ?? minimum, maximum: next.maximum ?? maximum });

  return <div className="desktop-menu" ref={menuRef}><span className="desktop-menu-label">{label}</span><button type="button" className="desktop-menu-trigger" aria-expanded={open} onClick={() => setOpen((current) => !current)}><span>{metricFilterLabel(operator, minimum, maximum, unit)}</span><b>⌄</b></button>{open ? <div className="desktop-menu-popover metric-popover"><div className="desktop-operator-grid">{Object.values(MetricOperator).map((item) => <button type="button" className={operator === item ? "selected" : ""} key={item} onClick={() => update({ operator: item })}>{metricOperatorLabel(item)}</button>)}</div>{operator !== MetricOperator.不限 ? <div className={`desktop-metric-inputs${operator === MetricOperator.区间 ? " range" : ""}`}><label><span>{operator === MetricOperator.小于等于 ? "最大值" : "最小值"}</span><input inputMode="decimal" value={operator === MetricOperator.小于等于 ? maximum : minimum} onChange={(event) => operator === MetricOperator.小于等于 ? update({ maximum: numericOnly(event.target.value) }) : update({ minimum: numericOnly(event.target.value) })} placeholder="请输入数值" /><small>{unit}</small></label>{operator === MetricOperator.区间 ? <label><span>最大值</span><input inputMode="decimal" value={maximum} onChange={(event) => update({ maximum: numericOnly(event.target.value) })} placeholder="请输入数值" /><small>{unit}</small></label> : null}</div> : null}<div className="desktop-quick-values"><span>常用阈值</span><div>{quickValues.map((value) => <button type="button" key={value} className={operator === MetricOperator.大于等于 && minimum === value ? "selected" : ""} onClick={() => { onChange({ operator: MetricOperator.大于等于, minimum: value, maximum: "" }); setOpen(false); }}>≥ {formatMetricValue(value)}{unit}</button>)}</div></div></div> : null}</div>;
}

/** 在点击浮层外部或按 Escape 时关闭当前桌面下拉，保持鼠标键盘交互完整。 */
function useDesktopMenuDismiss(menuRef: RefObject<HTMLDivElement | null>, open: boolean, onDismiss: () => void) {
  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) onDismiss(); };
    const handleKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onDismiss(); };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => { document.removeEventListener("mousedown", handlePointerDown); document.removeEventListener("keydown", handleKeyDown); };
  }, [menuRef, onDismiss, open]);
}

/** 将 H5 与桌面端共用的数值条件转换为紧凑中文展示文案。 */
function metricFilterLabel(operator: MetricOperator, minimum: string, maximum: string, unit: string) { if (operator === MetricOperator.不限) return "不限"; if (operator === MetricOperator.大于等于) return `≥ ${formatMetricValue(minimum)}${unit}`; if (operator === MetricOperator.小于等于) return `≤ ${formatMetricValue(maximum)}${unit}`; return `${formatMetricValue(minimum)}-${formatMetricValue(maximum)}${unit}`; }

/** 将数值筛选的内部稳定值转换为用户可读的中文比较方式。 */
function metricOperatorLabel(operator: MetricOperator) { if (operator === MetricOperator.不限) return "不限"; if (operator === MetricOperator.大于等于) return "大于等于"; if (operator === MetricOperator.小于等于) return "小于等于"; return "区间"; }

/** 将排序的内部字段值转换成桌面下拉中显示的中文业务名称。 */
function desktopMenuOptionLabel(label: string, value: string) { return label === "排序" ? sortFieldLabel(value as SortField) : value; }

/** 保持排序枚举稳定值不变，仅为界面提供中文文案。 */
function sortFieldLabel(field: SortField) { if (field === SortField.排名) return "排名"; if (field === SortField.用户支付金额) return "用户支付金额"; return "成交件数"; }

/** 使用中文万单位缩短大额指标，避免桌面触发器出现长数字。 */
function formatMetricValue(value: string) { const numeric = Number(value); return Number.isFinite(numeric) && numeric >= 10_000 ? `${numeric / 10_000}万` : value || "不限"; }

/** 清理桌面指标输入，仅保留可被既有筛选逻辑解析的数字和小数点。 */
function numericOnly(value: string) { return value.replace(/[^\d.]/g, ""); }
