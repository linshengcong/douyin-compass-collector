import { useEffect } from "react";
import type { ReactNode } from "react";

interface BottomSheetProps {
  title: string;
  children: ReactNode;
  onClose: () => void;
  onConfirm?: () => void;
  rightAction?: { label: string; onClick: () => void };
  nested?: boolean;
}

/** 提供移动端筛选共用的遮罩、底部滑入动画和确认边界。 */
export function BottomSheet({ title, children, onClose, onConfirm, rightAction, nested = false }: BottomSheetProps) {
  useEffect(() => {
    // Escape 与遮罩关闭语义一致，均不提交弹层内的临时选择。
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className={`sheet-overlay${nested ? " nested-sheet" : ""}`} role="presentation" onMouseDown={onClose}>
      <section className="bottom-sheet" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => event.stopPropagation()}>
        <header className="sheet-header">
          <button type="button" className="sheet-close" onClick={onClose} aria-label="关闭筛选">×</button>
          <strong>{title}</strong>
          {rightAction ? <button type="button" className="sheet-confirm" onClick={rightAction.onClick}>{rightAction.label}</button> : onConfirm ? <button type="button" className="sheet-confirm" onClick={onConfirm}>确认</button> : <span />}
        </header>
        <div className="sheet-content">{children}</div>
      </section>
    </div>
  );
}
