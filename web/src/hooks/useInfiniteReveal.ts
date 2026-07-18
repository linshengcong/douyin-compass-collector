import { useEffect, useRef, useState } from "react";

/** 按固定批次向移动端列表追加已在浏览器加载完成的数据。 */
export function useInfiniteReveal<T>(items: T[], batchSize = 50) {
  // visibleCount 只控制渲染数量，不会改变已下载的公开快照。
  const [visibleCount, setVisibleCount] = useState(batchSize);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // 任意筛选结果变化都回到首批，但不改变页面当前滚动位置。
    setVisibleCount(batchSize);
  }, [batchSize, items]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || visibleCount >= items.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setVisibleCount((current) => Math.min(current + batchSize, items.length));
        }
      },
      { rootMargin: "200px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [batchSize, items.length, visibleCount]);

  return {
    visibleItems: items.slice(0, visibleCount),
    visibleCount: Math.min(visibleCount, items.length),
    sentinelRef,
  };
}
