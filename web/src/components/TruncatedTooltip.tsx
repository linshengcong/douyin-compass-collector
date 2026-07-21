import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

interface TruncatedTooltipProps {
  /** 需要在 PC 表格中完整保留、但可能被省略显示的业务文本。 */
  text: string;
  /** 调用方附加的列内文本样式类名。 */
  className?: string;
}

interface TooltipPosition {
  /** 相对浏览器视口的提示框水平锚点。 */
  left: number;
  /** 相对浏览器视口的提示框垂直锚点。 */
  top: number;
}

/** 仅在单行文本被视觉省略时，通过页面级浮层展示其完整内容。 */
export function TruncatedTooltip({ text, className = "" }: TruncatedTooltipProps) {
  const textRef = useRef<HTMLSpanElement>(null);
  // 文本省略状态由真实渲染宽度决定，避免短文本出现无意义提示。
  const [isTruncated, setTruncated] = useState(false);
  const [position, setPosition] = useState<TooltipPosition | null>(null);

  /** 重新测量单行内容是否超出可用宽度，并返回当前判断结果。 */
  const measureTruncation = () => {
    const element = textRef.current;
    const nextValue = Boolean(element && element.scrollWidth > element.clientWidth);
    setTruncated(nextValue);
    return nextValue;
  };

  useEffect(() => {
    // 表格宽度会随窗口和横向滚动容器变化，观察尺寸以维持判断准确。
    const element = textRef.current;
    if (!element) return undefined;
    measureTruncation();
    const observer = new ResizeObserver(measureTruncation);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text]);

  /** 只有在文本已被省略时才记录浮层位置并展示完整文案。 */
  const showTooltip = () => {
    if (!measureTruncation()) return;
    const bounds = textRef.current?.getBoundingClientRect();
    if (bounds) setPosition({ left: bounds.left + (bounds.width / 2), top: bounds.top });
  };

  return <><span ref={textRef} className={`truncated-tooltip-trigger ${className}`} tabIndex={isTruncated ? 0 : undefined} onMouseEnter={showTooltip} onMouseLeave={() => setPosition(null)} onFocus={showTooltip} onBlur={() => setPosition(null)}>{text}</span>{position ? createPortal(<span className="truncated-tooltip" role="tooltip" style={{ left: position.left, top: position.top }}>{text}</span>, document.body) : null}</>;
}
