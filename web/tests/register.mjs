import { register } from "node:module";
// 使用已有 TypeScript 编译器加载测试源码，不新增构建或测试运行框架。
register("./typescript-loader.mjs", import.meta.url);
