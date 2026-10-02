import { useEffect, useState } from "react";
import { RankingPlatform, type LatestIndex, type RankingRecord } from "../types";
import { loadRankingData } from "../lib/data";

/** 请求公开 latest 索引及其不可变榜单快照。 */
export function useRankingData(dataIndexUrl: string | undefined, platform: RankingPlatform) {
  // records、index、loading 与 loadError 共同描述远端公开快照状态。
  const [records, setRecords] = useState<RankingRecord[]>([]);
  const [index, setIndex] = useState<LatestIndex | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    // 组件卸载后不再写入异步请求的结果。
    let cancelled = false;
    // 平台切换或重试时清空旧快照与错误，避免上一平台闪现。
    const controller = new AbortController();
    setRecords([]);
    setIndex(null);
    setLoadError(null);
    setLoading(true);
    if (!dataIndexUrl) {
      setLoadError("尚未配置网页数据地址");
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }
    void (async () => {
      try {
        // 读取边界统一核对平台、任务、批次、版本及数量。
        const { index: latest, records: rows } = await loadRankingData(dataIndexUrl, platform, controller.signal);
        if (!cancelled) {
          setIndex(latest);
          setRecords(rows);
        }
      } catch {
        if (!cancelled) setLoadError("暂时无法读取最新榜单，请稍后刷新重试");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [dataIndexUrl, platform]);

  return { records, index, loading, loadError };
}
