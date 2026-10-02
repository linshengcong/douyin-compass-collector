import { createRoot } from "react-dom/client";
import { RankingApp } from "./components/RankingApp";
import { RankingPlatform } from "./types";
import "./styles/shared.css";
import "./styles/desktop.css";
import "./styles/mobile.css";

// 抖音保留既有环境变量，淘宝使用独立索引，不继承旧数据地址。
const dataIndexUrl = import.meta.env.VITE_DATA_INDEX_URL as string | undefined;
const taobaoDataIndexUrl = import.meta.env.VITE_TAOBAO_DATA_INDEX_URL as string | undefined;

// 只有显式淘宝值改变首屏平台，CI和既有公开站默认行为保持抖音。
const initialPlatform = import.meta.env.VITE_DEFAULT_PLATFORM === "taobao" ? RankingPlatform.淘宝 : RankingPlatform.抖音;
createRoot(document.getElementById("root")!).render(<RankingApp dataIndexUrl={dataIndexUrl} taobaoDataIndexUrl={taobaoDataIndexUrl} initialPlatform={initialPlatform} />);
