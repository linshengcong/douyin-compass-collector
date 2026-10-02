import { createRoot } from "react-dom/client";
import { RankingApp } from "./components/RankingApp";
import "./styles/shared.css";
import "./styles/desktop.css";
import "./styles/mobile.css";

// 抖音保留既有环境变量，淘宝使用独立索引，不继承旧数据地址。
const dataIndexUrl = import.meta.env.VITE_DATA_INDEX_URL as string | undefined;
const taobaoDataIndexUrl = import.meta.env.VITE_TAOBAO_DATA_INDEX_URL as string | undefined;

createRoot(document.getElementById("root")!).render(<RankingApp dataIndexUrl={dataIndexUrl} taobaoDataIndexUrl={taobaoDataIndexUrl} />);
