import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite 只构建 web/ 目录，适合在当前 Python 仓库内由 Vercel 单独托管。
export default defineConfig({
  plugins: [react()],
});
