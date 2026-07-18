import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { DesktopRankingView } from "./components/DesktopRankingView";
import { MobileRankingView } from "./components/MobileRankingView";
import { useRankingData } from "./hooks/useRankingData";
import { useRankingFilters } from "./hooks/useRankingFilters";
import type { RankingFilters } from "./types";
import "./styles/shared.css";
import "./styles/desktop.css";
import "./styles/mobile.css";

// Vercel 和本地开发都通过同一环境变量定位 OSS 公共快照索引。
const dataIndexUrl = import.meta.env.VITE_DATA_INDEX_URL as string | undefined;

/** 协调公开快照、共享筛选状态与两套响应式视图。 */
function RankingApp() {
  const { records, index, loading, loadError } = useRankingData(dataIndexUrl);
  // 桌面端沿用即时筛选；移动端在综合弹层点击应用后才提交同一份筛选状态。
  const ranking = useRankingFilters(records, 0);
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
    <DesktopRankingView records={ranking.filteredRecords} filters={ranking.filters} options={options} page={page} pageSize={pageSize} totalPages={totalPages} onSetFilters={setFilters} onSelectLevel1={ranking.selectLevel1} onSelectLevel2={ranking.selectLevel2} onSelectLevel3={ranking.selectLevel3} onPageChange={setPage} onPageSizeChange={setPageSize} onReset={ranking.resetFilters} />
    <MobileRankingView snapshotRecords={records} records={ranking.filteredRecords} index={index} filters={ranking.filters} onSetFilters={setFilters} />
  </main>;
}

createRoot(document.getElementById("root")!).render(<RankingApp />);
