/** 渲染当前结果顺序的排名标识，前三名保持克制的视觉强调。 */
export function RankMark({ rank }: { rank: number }) {
  return <span className={`rank-mark rank-${Math.min(rank, 4)}`}>{rank}</span>;
}
