import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite 只构建 web/ 目录，适合在当前 Python 仓库内由 Vercel 单独托管。
export default defineConfig({
  plugins: [react()],
  // GitHub 用户主页仓库在根域名发布，无需附加当前仓库子路径。
  base: "/",
  // 本地开发与 Vercel 使用同一仓库根目录 .env；Vite 仅向浏览器暴露 VITE_ 前缀变量。
  envDir: "..",
});
