import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import ts from "typescript";

/** 仅将源码的无后缀相对导入解析为 TS 文件，第三方依赖仍由 Node 处理。 */
export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (error) {
    if (specifier.startsWith(".") && !/\.[a-z]+$/i.test(specifier)) {
      for (const extension of [".ts", ".tsx"]) {
        try { return await nextResolve(specifier + extension, context); } catch { /* 尝试另一个源码后缀。 */ }
      }
    }
    throw error;
  }
}

/** 测试使用项目现有 TS 编译器，生产类型检查仍由 npm run build 执行。 */
export async function load(url, context, nextLoad) {
  if (/\.tsx?$/.test(url)) {
    // 转译只供 Node 测试执行，正式 bundle 仍沿用 Vite。
    const source = await readFile(fileURLToPath(url), "utf8");
    return { format: "module", shortCircuit: true, source: ts.transpileModule(source, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
    }).outputText };
  }
  return nextLoad(url, context);
}
