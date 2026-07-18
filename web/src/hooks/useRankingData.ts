import { useEffect, useState } from "react";
import type { DataSnapshot, LatestIndex, RankingRecord } from "../types";

/** 请求公开 latest 索引及其不可变榜单快照。 */
export function useRankingData(dataIndexUrl: string | undefined) {
  // records、index、loading 与 loadError 共同描述远端公开快照状态。
  const [records, setRecords] = useState<RankingRecord[]>([]);
  const [index, setIndex] = useState<LatestIndex | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    // 组件卸载后不再写入异步请求的结果。
    let cancelled = false;
    if (!dataIndexUrl) {
      setLoadError("尚未配置网页数据地址");
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }
    void (async () => {
      try {
        const indexResponse = await fetch(dataIndexUrl, { cache: "no-store" });
        if (!indexResponse.ok) throw new Error("latest index unavailable");
        const latest = (await indexResponse.json()) as LatestIndex;
        const dataResponse = await fetch(latest.data_url, { cache: "no-store" });
        if (!dataResponse.ok) throw new Error("snapshot unavailable");
        const snapshot = (await dataResponse.json()) as DataSnapshot;
        if (!Array.isArray(snapshot.records)) throw new Error("snapshot contract invalid");
        if (!cancelled) {
          setIndex(latest);
          setRecords(snapshot.records);
        }
      } catch {
        if (!cancelled) setLoadError("暂时无法读取最新榜单，请稍后刷新重试");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [dataIndexUrl]);

  return { records, index, loading, loadError };
}
