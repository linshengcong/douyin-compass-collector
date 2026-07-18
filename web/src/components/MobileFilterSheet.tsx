import { useEffect, useState } from "react";
import { BottomSheet } from "./BottomSheet";

export type MobileFilterKind = "level1" | "level2" | "level3" | "pay" | "count";

interface MobileFilterSheetProps {
  kind: MobileFilterKind;
  title: string;
  value: string;
  options?: string[];
  onClose: () => void;
  onConfirm: (value: string) => void;
}

const QUICK_VALUES = {
  pay: ["", "1000", "10000", "100000"],
  count: ["", "10", "100", "1000"],
} as const;

/** 处理类目单选与数值阈值两种移动端筛选弹层。 */
export function MobileFilterSheet({ kind, title, value, options = [], onClose, onConfirm }: MobileFilterSheetProps) {
  // draftValue 只在确认时回写共享筛选状态，关闭弹层不会意外修改列表。
  const [draftValue, setDraftValue] = useState(value);
  const isNumeric = kind === "pay" || kind === "count";
  const unit = kind === "pay" ? "元" : "件";
  // 仅数值字段读取对应快捷值，避免类目字段混入数值选择逻辑。
  const quickValues = kind === "pay" ? QUICK_VALUES.pay : QUICK_VALUES.count;

  useEffect(() => {
    // 重新打开或切换字段时使用当前已确认值初始化临时选择。
    setDraftValue(value);
  }, [kind, value]);

  return (
    <BottomSheet title={title} onClose={onClose} onConfirm={() => onConfirm(draftValue)}>
      {isNumeric ? (
        <div className="sheet-numeric">
          <label>
            <span>最低阈值</span>
            <div><b>≥</b><input autoFocus inputMode="decimal" value={draftValue} onChange={(event) => setDraftValue(event.target.value.replace(/[^\d.]/g, ""))} placeholder="不限" /><small>{unit}</small></div>
          </label>
          <p>快捷选择</p>
          <div className="quick-values">
            {quickValues.map((item) => <button type="button" className={draftValue === item ? "selected" : ""} key={item || "all"} onClick={() => setDraftValue(item)}>{item ? `≥ ${Number(item).toLocaleString()} ${unit}` : "不限"}</button>)}
          </div>
        </div>
      ) : (
        <div className="sheet-options">
          {options.map((option) => <button type="button" className={draftValue === option ? "selected" : ""} key={option} onClick={() => setDraftValue(option)}><span>{option}</span>{draftValue === option ? <b>✓</b> : null}</button>)}
        </div>
      )}
    </BottomSheet>
  );
}
