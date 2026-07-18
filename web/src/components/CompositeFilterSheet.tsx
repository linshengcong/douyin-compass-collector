import { useMemo, useState } from "react";
import { BottomSheet } from "./BottomSheet";
import { MetricOperator, SortDirection, SortField, type RankingFilters, type RankingRecord } from "../types";

type CategoryLevel = "level1" | "level2" | "level3";
type PickerKind = CategoryLevel | "sort" | "pay" | "count";

// 排序枚举值服务于筛选计算，界面始终展示用户可读的中文文案。
const sortFieldLabels: Record<SortField, string> = {
  [SortField.排名]: "排名",
  [SortField.用户支付金额]: "用户支付金额",
  [SortField.成交件数]: "成交件数",
};

interface CompositeFilterSheetProps {
  records: RankingRecord[];
  filters: RankingFilters;
  onClose: () => void;
  onApply: (filters: RankingFilters) => void;
  onReset: () => void;
}

/** 集中承载移动端筛选草稿，只有应用或重置才会将状态提交给榜单。 */
export function CompositeFilterSheet({ records, filters, onClose, onApply, onReset }: CompositeFilterSheetProps) {
  // draft 与已应用 filters 分离，避免弹层内操作触发底层列表变化。
  const [draft, setDraft] = useState<RankingFilters>(filters);
  const [pickerKind, setPickerKind] = useState<PickerKind | null>(null);
  const options = useMemo(() => buildCategoryOptions(records, draft), [draft, records]);

  /** 应用一级、二级、三级选择，并在父级变化时清空后代草稿。 */
  const selectCategory = (level: CategoryLevel, value: string) => {
    setDraft((current) => {
      if (level === "level1") return { ...current, level1: value, level2: "全部", level3: "全部" };
      if (level === "level2") return { ...current, level2: value, level3: "全部" };
      return { ...current, level3: value };
    });
    setPickerKind(null);
  };

  return <>
    <BottomSheet title="筛选条件" onClose={onClose} rightAction={{ label: "重置", onClick: onReset }}>
      <div className="composite-filter">
        <label className="composite-search"><span>⌕</span><input autoFocus value={draft.keyword} onChange={(event) => setDraft((current) => ({ ...current, keyword: event.target.value }))} placeholder="搜索商品名称或店铺名称" /></label>
        <div className="composite-category-grid">
          <CategoryTrigger label="一级类目" value={draft.level1} onClick={() => setPickerKind("level1")} />
          <CategoryTrigger label="二级类目" value={draft.level2} onClick={() => setPickerKind("level2")} />
          <CategoryTrigger label="三级类目" value={draft.level3} onClick={() => setPickerKind("level3")} />
        </div>
        <div className="composite-value-grid">
          <CategoryTrigger label="支付金额" value={metricFilterLabel(draft.payOperator, draft.payMinimum, draft.payMaximum, "元")} onClick={() => setPickerKind("pay")} />
          <CategoryTrigger label="成交件数" value={metricFilterLabel(draft.countOperator, draft.countMinimum, draft.countMaximum, "件")} onClick={() => setPickerKind("count")} />
        </div>
        <div className="composite-bottom-grid">
          <CategoryTrigger label="排序" value={sortFieldLabel(draft.sortField)} onClick={() => setPickerKind("sort")} />
          <button type="button" className="direction-button" onClick={() => setDraft((current) => ({ ...current, sortDirection: current.sortDirection === SortDirection.升序 ? SortDirection.降序 : SortDirection.升序 }))}>{draft.sortDirection === SortDirection.升序 ? "↑ 升序" : "↓ 降序"}</button>
          <label className="new-only-field"><span>仅首次上榜</span><input type="checkbox" checked={draft.newOnly} onChange={(event) => setDraft((current) => ({ ...current, newOnly: event.target.checked }))} /><b>{draft.newOnly ? "已选中" : "未选中"}</b><small>核心关注条件，已为您优先筛选</small></label>
        </div>
        <div className="draft-summary"><span>当前已选</span>{summaryLabels(draft).map((label) => <b key={label}>{label}</b>)}</div>
        <div className="composite-actions"><button type="button" onClick={onReset}>重置</button><button type="button" className="apply-filter" onClick={() => onApply(draft)}>应用筛选 <small>共 {filterCount(records, draft).toLocaleString()} 条结果</small></button></div>
      </div>
    </BottomSheet>
    {pickerKind === "sort" ? <SortPicker value={draft.sortField} onClose={() => setPickerKind(null)} onConfirm={(sortField) => { setDraft((current) => ({ ...current, sortField })); setPickerKind(null); }} /> : null}
    {pickerKind === "pay" || pickerKind === "count" ? <MetricPicker title={pickerKind === "pay" ? "支付金额" : "成交件数"} unit={pickerKind === "pay" ? "元" : "件"} quickValues={pickerKind === "pay" ? ["10000", "50000", "100000"] : ["10", "50", "100"]} operator={pickerKind === "pay" ? draft.payOperator : draft.countOperator} minimum={pickerKind === "pay" ? draft.payMinimum : draft.countMinimum} maximum={pickerKind === "pay" ? draft.payMaximum : draft.countMaximum} onClose={() => setPickerKind(null)} onConfirm={(metric) => { setDraft((current) => pickerKind === "pay" ? { ...current, payOperator: metric.operator, payMinimum: metric.minimum, payMaximum: metric.maximum } : { ...current, countOperator: metric.operator, countMinimum: metric.minimum, countMaximum: metric.maximum }); setPickerKind(null); }} /> : null}
    {pickerKind === "level1" || pickerKind === "level2" || pickerKind === "level3" ? <CategoryPicker level={pickerKind} value={draft[pickerKind]} options={options[pickerKind]} onClose={() => setPickerKind(null)} onConfirm={(value) => selectCategory(pickerKind, value)} /> : null}
  </>;
}

/** 子 Popup 只负责单层类目选择，确认后返回仍在打开的综合筛选层。 */
function CategoryPicker({ level, value, options, onClose, onConfirm }: { level: CategoryLevel; value: string; options: string[]; onClose: () => void; onConfirm: (value: string) => void }) {
  const [selected, setSelected] = useState(value);
  const label = level === "level1" ? "一级类目" : level === "level2" ? "二级类目" : "三级类目";
  return <BottomSheet title={`选择${label}`} nested onClose={onClose} onConfirm={() => onConfirm(selected)}><div className="sheet-options">{options.map((option) => <button type="button" className={selected === option ? "selected" : ""} key={option} onClick={() => setSelected(option)}><span>{option}</span>{selected === option ? <b>✓</b> : null}</button>)}</div></BottomSheet>;
}

/** 排序字段使用与类目一致的独立选择 Popup，避免原生 select 的平台差异。 */
function SortPicker({ value, onClose, onConfirm }: { value: SortField; onClose: () => void; onConfirm: (value: SortField) => void }) {
  const [selected, setSelected] = useState(value);
  return <BottomSheet title="选择排序" nested onClose={onClose} onConfirm={() => onConfirm(selected)}><div className="sheet-options">{Object.values(SortField).map((field) => <button type="button" className={selected === field ? "selected" : ""} key={field} onClick={() => setSelected(field)}><span>{sortFieldLabel(field)}</span>{selected === field ? <b>✓</b> : null}</button>)}</div></BottomSheet>;
}

/** 数值 Popup 支持常用阈值和自定义大于、小于、区间筛选。 */
function MetricPicker({ title, unit, quickValues, operator, minimum, maximum, onClose, onConfirm }: { title: string; unit: string; quickValues: string[]; operator: MetricOperator; minimum: string; maximum: string; onClose: () => void; onConfirm: (value: { operator: MetricOperator; minimum: string; maximum: string }) => void }) {
  const [draftOperator, setDraftOperator] = useState(operator);
  const [draftMinimum, setDraftMinimum] = useState(minimum);
  const [draftMaximum, setDraftMaximum] = useState(maximum);
  /** 选择快捷阈值时统一转换为大于等于条件。 */
  const chooseQuickValue = (value: string) => { setDraftOperator(MetricOperator.大于等于); setDraftMinimum(value); setDraftMaximum(""); };
  return <BottomSheet title={title} nested onClose={onClose} onConfirm={() => onConfirm({ operator: draftOperator, minimum: draftMinimum, maximum: draftMaximum })}><div className="metric-picker"><div className="metric-operator-grid">{Object.values(MetricOperator).map((item) => <button type="button" key={item} className={draftOperator === item ? "selected" : ""} onClick={() => setDraftOperator(item)}>{metricOperatorLabel(item)}</button>)}</div>{draftOperator !== MetricOperator.不限 ? <div className={draftOperator === MetricOperator.区间 ? "metric-inputs range" : "metric-inputs"}><label><span>{draftOperator === MetricOperator.小于等于 ? "最大值" : "最小值"}</span><input inputMode="decimal" value={draftOperator === MetricOperator.小于等于 ? draftMaximum : draftMinimum} onChange={(event) => draftOperator === MetricOperator.小于等于 ? setDraftMaximum(numericOnly(event.target.value)) : setDraftMinimum(numericOnly(event.target.value))} placeholder="请输入数值" /><small>{unit}</small></label>{draftOperator === MetricOperator.区间 ? <label><span>最大值</span><input inputMode="decimal" value={draftMaximum} onChange={(event) => setDraftMaximum(numericOnly(event.target.value))} placeholder="请输入数值" /><small>{unit}</small></label> : null}</div> : null}<section className="quick-metrics"><span>常用选项</span><div>{quickValues.map((value) => <button type="button" key={value} className={draftOperator === MetricOperator.大于等于 && draftMinimum === value ? "selected" : ""} onClick={() => chooseQuickValue(value)}>≥ {formatMetricValue(value)}{unit}</button>)}</div></section></div></BottomSheet>;
}

/** 表达只读筛选 input 的触发器，实际选择在二级 Popup 完成。 */
function CategoryTrigger({ label, value, onClick }: { label: string; value: string; onClick: () => void }) { return <button type="button" className="composite-trigger" onClick={onClick}><small>{label}</small><span>{value}</span><b>⌄</b></button>; }

/** 根据草稿动态生成当前层级可选项，保证二级 Popup 不展示无效分类。 */
function buildCategoryOptions(records: RankingRecord[], draft: RankingFilters) {
  const unique = (items: RankingRecord[], key: "level1" | "level2" | "level3") => ["全部", ...Array.from(new Set(items.map((item) => item[key])))] as string[];
  const level1 = unique(records, "level1");
  const level2Records = draft.level1 === "全部" ? records : records.filter((item) => item.level1 === draft.level1);
  const level3Records = level2Records.filter((item) => draft.level2 === "全部" || item.level2 === draft.level2);
  return { level1, level2: unique(level2Records, "level2"), level3: unique(level3Records, "level3") };
}

/** 生成可读筛选摘要，便于用户在应用前检查草稿。 */
function summaryLabels(draft: RankingFilters) { return [`一级类目：${draft.level1}`, `支付金额：${metricFilterLabel(draft.payOperator, draft.payMinimum, draft.payMaximum, "元")}`, draft.newOnly ? "仅首次上榜" : "全部商品", `排序：${sortFieldLabel(draft.sortField)}（${draft.sortDirection === SortDirection.升序 ? "升序" : "降序"}）`]; }

/** 将稳定排序字段转换为用户界面使用的中文名称。 */
function sortFieldLabel(field: SortField) { return sortFieldLabels[field]; }

/** 将数值比较方式转换为 Popup 中可读的操作文案。 */
function metricOperatorLabel(operator: MetricOperator) { if (operator === MetricOperator.不限) return "不限"; if (operator === MetricOperator.大于等于) return "大于等于"; if (operator === MetricOperator.小于等于) return "小于等于"; return "区间"; }

/** 将数值筛选状态压缩为按钮和摘要中使用的简短文案。 */
function metricFilterLabel(operator: MetricOperator, minimum: string, maximum: string, unit: string) { if (operator === MetricOperator.不限) return "不限"; if (operator === MetricOperator.大于等于) return `≥ ${formatMetricValue(minimum)}${unit}`; if (operator === MetricOperator.小于等于) return `≤ ${formatMetricValue(maximum)}${unit}`; return `${formatMetricValue(minimum)}-${formatMetricValue(maximum)}${unit}`; }

/** 将较大数值缩写为万，避免移动端触发器文案溢出。 */
function formatMetricValue(value: string) { const numeric = Number(value); return Number.isFinite(numeric) && numeric >= 10_000 ? `${numeric / 10_000}万` : value || "不限"; }

/** 清理数值输入，只保留用于阈值计算的数字和小数点。 */
function numericOnly(value: string) { return value.replace(/[^\d.]/g, ""); }

/** 仅用于应用按钮的结果预览，实际筛选仍由共享 Hook 在提交后执行。 */
function filterCount(records: RankingRecord[], draft: RankingFilters) { const keyword = draft.keyword.trim().toLocaleLowerCase(); return records.filter((item) => (!keyword || item.product_name.toLocaleLowerCase().includes(keyword) || item.shop_name.toLocaleLowerCase().includes(keyword)) && (draft.level1 === "全部" || item.level1 === draft.level1) && (draft.level2 === "全部" || item.level2 === draft.level2) && (draft.level3 === "全部" || item.level3 === draft.level3) && (!draft.newOnly || item.newly_on_ranking) && matchesMetricFilter(numberFloor(item.pay_amount), draft.payOperator, draft.payMinimum, draft.payMaximum) && matchesMetricFilter(numberFloor(item.pay_combo_count), draft.countOperator, draft.countMinimum, draft.countMaximum)).length; }

/** 在筛选预览中复用数值比较规则，保持按钮计数与实际列表一致。 */
function matchesMetricFilter(value: number, operator: MetricOperator, minimum: string, maximum: string) { const min = Number(minimum); const max = Number(maximum); if (operator === MetricOperator.大于等于) return !Number.isFinite(min) || value >= min; if (operator === MetricOperator.小于等于) return !Number.isFinite(max) || value <= max; if (operator === MetricOperator.区间) return (!Number.isFinite(min) || value >= min) && (!Number.isFinite(max) || value <= max); return true; }

/** 解析展示区间的最低数值，使预览计数与既有筛选口径一致。 */
function numberFloor(value: string) { const matched = value.replaceAll("¥", "").match(/([\d.]+)(亿|万)?/); if (!matched) return 0; return Number(matched[1]) * (matched[2] === "亿" ? 100_000_000 : matched[2] === "万" ? 10_000 : 1); }
