import { useEffect, useState } from "react";
import { DesktopRankingView } from "./DesktopRankingView";
import { MobileRankingView } from "./MobileRankingView";
import { useRankingData } from "../hooks/useRankingData";
import { useRankingFilters } from "../hooks/useRankingFilters";
import { RankingPlatform, type RankingFilters } from "../types";

/** 切换控件独立于加载结果，错误与空配置状态仍允许切回其他平台。 */
export function RankingApp({ dataIndexUrl, taobaoDataIndexUrl }: { dataIndexUrl?: string; taobaoDataIndexUrl?: string }) {
  // 默认抖音； keyed 子组件确保筛选、分页、弹层与移动展开全部重建。
  const [platform, setPlatform] = useState(RankingPlatform.抖音);
  useEffect(() => {
    // 标签页标题随当前平台同步，避免淘宝榜单仍显示抖音标题。
    document.title = `${platform === RankingPlatform.淘宝 ? "淘宝" : "抖音"}商品实时榜`;
  }, [platform]);
  return <>
    <nav className="platform-switch" aria-label="选择排行榜平台">
      {Object.entries(RankingPlatform).map(([label, value]) => <button type="button" key={value} aria-pressed={platform === value} onClick={() => setPlatform(value)}>{label}</button>)}
    </nav>
    <PlatformRankingApp key={platform} platform={platform} dataIndexUrl={platform === RankingPlatform.淘宝 ? taobaoDataIndexUrl : dataIndexUrl} />
  </>;
}

/** 协调公开快照、共享筛选状态与两套响应式视图。 */
function PlatformRankingApp({ platform, dataIndexUrl }: { platform: RankingPlatform; dataIndexUrl?: string }) {
  const { records, index, loading, loadError } = useRankingData(dataIndexUrl, platform);
  // 桌面端沿用即时筛选；移动端在综合弹层点击应用后才提交同一份筛选状态。
  const ranking = useRankingFilters(records, 0, platform);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const totalPages = Math.max(1, Math.ceil(ranking.filteredRecords.length / pageSize));

  useEffect(() => {
    // 筛选或排序变化后仅桌面分页回到首屏；移动端保留用户当前列表滚动位置。
    setPage(1);
  }, [ranking.filters, pageSize]);

  if (loading) return <main className="state-card">正在加载最新榜单…</main>;
  if (loadError) return <main className="state-card error-state">{loadError}</main>;

  const options = { level1: ranking.level1Options, level2: ranking.level2Options, level3: ranking.level3Options };
  const setFilters = (updater: (current: RankingFilters) => RankingFilters) => ranking.setFilters(updater);

  return <main className="page-shell">
    {platform === RankingPlatform.淘宝 && <p className="platform-notice">支付买家数与访客数保留原始区间；筛选、排序按区间下界计算，缺失指标显示为「-」。{index?.started_at && index?.finished_at && <span>采集时间（北京时间）：<time dateTime={index.started_at}>{formatCollectionTime(index.started_at)}</time> 至 <time dateTime={index.finished_at}>{formatCollectionTime(index.finished_at)}</time></span>}</p>}
    <DesktopRankingView platform={platform} records={ranking.filteredRecords} publishedAt={index?.published_at} filters={ranking.filters} options={options} page={page} pageSize={pageSize} totalPages={totalPages} onSetFilters={setFilters} onSelectLevel1={ranking.selectLevel1} onSelectLevel2={ranking.selectLevel2} onSelectLevel3={ranking.selectLevel3} onPageChange={setPage} onPageSizeChange={setPageSize} onReset={ranking.resetFilters} />
    <MobileRankingView platform={platform} snapshotRecords={records} records={ranking.filteredRecords} index={index} filters={ranking.filters} onSetFilters={setFilters} />
  </main>;
}

/** 将已验证的公开时间转换为北京时间，保持秒精度与原始 time 属性。 */
function formatCollectionTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(new Date(value)).replaceAll("/", "-");
}
