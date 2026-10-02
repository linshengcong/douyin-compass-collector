import test from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { act, createElement } from "react";
import { RankingPlatform } from "../src/types.ts";
import { bundle } from "./fixtures.mjs";

// React DOM 的输入能力检测发生在模块加载时，先创建 DOM，再动态导入实际组件。
const bootstrapDOM = new JSDOM("<!doctype html>");
globalThis.window = bootstrapDOM.window;
globalThis.document = bootstrapDOM.window.document;
Object.defineProperty(globalThis, "navigator", { value: bootstrapDOM.window.navigator, configurable: true });
const { createRoot } = await import("react-dom/client");
const { RankingApp } = await import("../src/components/RankingApp.tsx");

/** 安装独立 DOM 与受控观察器，测试真实组件状态而非 HTML 字符串匹配。 */
function setup() {
  // 节点和观察器只存在于本地测试进程，不启动或控制用户浏览器。
  const dom = new JSDOM("<!doctype html><div id='root'></div>", { url: "https://example.invalid/" });
  const observers = [];
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node, IS_REACT_ACT_ENVIRONMENT: true, ResizeObserver: class {
    /** jsdom 没有真实尺寸变化，测试不验证截断布局。 */
    observe() {}
    /** 与生产卸载路径一致地提供断开入口。 */
    disconnect() {}
  }, IntersectionObserver: class {
    /** 保留回调，测试主动触发追加展示。 */
    constructor(callback) { this.callback = callback; observers.push(this); }
    /** DOM 观察只记录当前节点，不依赖页面真实布局。 */
    observe(node) { this.node = node; }
    /** 卸载后断开，避免上一平台的观察器驱动下一平台。 */
    disconnect() { this.node = null; }
  } })) { originals.set(key, globalThis[key]); globalThis[key] = value; }
  dom.window.scrollTo = () => {};
  // root 是真实 React DOM 挂载入口，与生产入口保持相同组件。
  const root = createRoot(dom.window.document.getElementById("root"));
  return { dom, root, observers, async cleanup() { await act(async () => root.unmount()); for (const [key, value] of originals) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } dom.window.close(); } };
}

/** 找到指定文本的真实按钮并经 DOM 分发点击事件。 */
async function click(dom, text) {
  // 从最新 DOM 解析按钮，不保留上一平台的旧节点。
  const button = [...dom.window.document.querySelectorAll("button")].find((node) => node.textContent === text);
  assert.ok(button, `missing button: ${text}`);
  await act(async () => { button.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); await new Promise((resolve) => setImmediate(resolve)); });
}

/** 用同一合成元信息响应两平台索引与快照。 */
function fixtureFetch(compass, taobao) {
  return async (url) => {
    // 根据独立地址选择平台，而不是用最后一次调用决定数据归属。
    const values = url.includes("/taobao/") ? taobao : compass;
    return { ok: true, json: async () => url.endsWith("latest.json") ? values.latest : values.snapshot };
  };
}

test("显式本地淘宝入口首屏只请求淘宝数据，仍能切回抖音", async () => {
  // React真实组件使用独立DOM，网络由合成快照替代。
  const environment = setup();
  const originalFetch = globalThis.fetch;
  // 请求顺序确认淘宝入口没有先展示或读取抖音快照。
  const requests = [];
  const respond = fixtureFetch(bundle(RankingPlatform.抖音), bundle(RankingPlatform.淘宝));
  globalThis.fetch = async (url) => { requests.push(url); return respond(url); };
  try {
    await act(async () => {
      environment.root.render(createElement(RankingApp, {
        dataIndexUrl: "https://example.invalid/compass/latest.json",
        taobaoDataIndexUrl: "https://example.invalid/taobao/latest.json", initialPlatform: RankingPlatform.淘宝,
      }));
      await new Promise((resolve) => setImmediate(resolve));
    });
    assert.ok(requests.length > 0 && requests.every((url) => url.includes("/taobao/")));
    assert.equal(environment.dom.window.document.title, "淘宝商品实时榜");
    assert.ok(environment.dom.window.document.body.textContent.includes("支付买家数"));
    await click(environment.dom, "抖音");
    assert.ok(requests.some((url) => url.includes("/compass/")));
    assert.equal(environment.dom.window.document.title, "抖音商品实时榜");
  } finally { globalThis.fetch = originalFetch; await environment.cleanup(); }
});

test("切换平台重置筛选、桌面分页、移动展示和首次上榜控件", async () => {
  // 六十条确保真实移动组件超过首批，而不是只测一个无状态商品。
  const environment = setup();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fixtureFetch(bundle(RankingPlatform.抖音, 60), bundle(RankingPlatform.淘宝, 60));
  try {
    await act(async () => { environment.root.render(createElement(RankingApp, { dataIndexUrl: "https://example.invalid/compass/latest.json", taobaoDataIndexUrl: "https://example.invalid/taobao/latest.json" })); await new Promise((resolve) => setImmediate(resolve)); });
    assert.ok(environment.dom.window.document.body.textContent.includes("仅首次上榜"));
    await act(async () => { environment.observers.at(-1).callback([{ isIntersecting: true }]); });
    assert.equal(environment.dom.window.document.querySelectorAll(".mobile-list article").length, 60);
    // 通过原生 setter 触发真实 React 输入变化，随后切换必须清空关键词。
    const input = environment.dom.window.document.querySelector(".search-field input");
    await act(async () => {
      Object.getOwnPropertyDescriptor(environment.dom.window.HTMLInputElement.prototype, "value").set.call(input, "compass");
      input.dispatchEvent(new environment.dom.window.Event("input", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 5));
    });
    assert.equal(input.value, "compass");
    await click(environment.dom, "2");
    await click(environment.dom, "淘宝");
    assert.equal(environment.dom.window.document.title, "淘宝商品实时榜");
    assert.ok(environment.dom.window.document.body.textContent.includes("采集时间（北京时间）"));
    assert.ok(environment.dom.window.document.body.textContent.includes("2026-10-01 14:00:00"));
    // 重建后的两端只含淘宝数据，桌面页码和移动展示回到默认。
    assert.equal(environment.dom.window.document.querySelectorAll(".mobile-list article").length, 50);
    assert.equal(environment.dom.window.document.querySelector(".active-page").textContent, "1");
    assert.equal(environment.dom.window.document.querySelector(".search-field input").value, "");
    assert.ok(!environment.dom.window.document.body.textContent.includes("首次上榜"));
    assert.ok(environment.dom.window.document.body.textContent.includes("支付买家数"));
    assert.ok(!environment.dom.window.document.body.textContent.includes("用户支付金额"));
    assert.ok(environment.dom.window.document.querySelector(".product-link").href.includes("mi_id=synthetic"));
    await click(environment.dom, "筛选 ⌘");
    assert.ok(!environment.dom.window.document.querySelector(".new-only-field"));
    await click(environment.dom, "抖音");
    assert.equal(environment.dom.window.document.title, "抖音商品实时榜");
    assert.equal(environment.dom.window.document.querySelectorAll(".mobile-list article").length, 50);
    assert.ok(environment.dom.window.document.body.textContent.includes("仅首次上榜"));
    assert.ok(!environment.dom.window.document.querySelector(".bottom-sheet"));
  } finally { globalThis.fetch = originalFetch; await environment.cleanup(); }
});

test("淘宝实体名称按文本显示且能搜索，不会生成名称中的HTML节点", async () => {
  // 从真实加载与React渲染链路验证实体解码，而非仅测试解码函数。
  const environment = setup();
  const originalFetch = globalThis.fetch;
  const taobao = bundle(RankingPlatform.淘宝, 3);
  taobao.snapshot.records[0].product_name = "观夏&times;Hirono &lt;img src=x onerror=alert(1)&gt;";
  taobao.snapshot.records[0].shop_name = "A&amp;B旗舰店";
  globalThis.fetch = fixtureFetch(bundle(RankingPlatform.抖音), taobao);
  try {
    await act(async () => { environment.root.render(createElement(RankingApp, { dataIndexUrl: "https://example.invalid/compass/latest.json", taobaoDataIndexUrl: "https://example.invalid/taobao/latest.json" })); await new Promise((resolve) => setImmediate(resolve)); });
    await click(environment.dom, "淘宝");
    assert.equal(environment.dom.window.document.querySelector(".product-link").textContent, "观夏×Hirono <img src=x onerror=alert(1)>");
    assert.ok(environment.dom.window.document.body.textContent.includes("A&B旗舰店"));
    assert.equal(environment.dom.window.document.querySelectorAll('img[src="x"]').length, 0);
    // 搜索使用同一规范化名称，同时原始快照不被显示层修改。
    const input = environment.dom.window.document.querySelector(".search-field input");
    await act(async () => {
      Object.getOwnPropertyDescriptor(environment.dom.window.HTMLInputElement.prototype, "value").set.call(input, "观夏×Hirono");
      input.dispatchEvent(new environment.dom.window.Event("input", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 5));
    });
    assert.ok(environment.dom.window.document.querySelector(".result-heading").textContent.includes("共 1 条结果"));
    assert.equal(taobao.snapshot.records[0].shop_name, "A&amp;B旗舰店");
  } finally { globalThis.fetch = originalFetch; await environment.cleanup(); }
});

test("淘宝未配置时切换入口仍可用并能返回抖音", async () => {
  // 未配置不能继承抖音地址，尤其不能显示抖音商品却标成淘宝。
  const environment = setup();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fixtureFetch(bundle(RankingPlatform.抖音), bundle(RankingPlatform.淘宝));
  try {
    await act(async () => { environment.root.render(createElement(RankingApp, { dataIndexUrl: "https://example.invalid/compass/latest.json" })); await new Promise((resolve) => setImmediate(resolve)); });
    await click(environment.dom, "淘宝");
    assert.ok(environment.dom.window.document.body.textContent.includes("尚未配置网页数据地址"));
    assert.equal(environment.dom.window.document.querySelectorAll(".platform-switch button").length, 2);
    await click(environment.dom, "抖音");
    assert.ok(environment.dom.window.document.body.textContent.includes("compass合成商品1"));
    assert.ok(!environment.dom.window.document.body.textContent.includes("尚未配置"));
  } finally { globalThis.fetch = originalFetch; await environment.cleanup(); }
});


test("加载与错误状态仍能切换，旧平台迟到结果不会覆盖当前平台", async () => {
  // 淘宝索引故意延迟，并在返回抖音后才完成，覆盖真实组件卸载与取消边界。
  const environment = setup();
  const originalFetch = globalThis.fetch;
  const compass = bundle(RankingPlatform.抖音);
  const taobao = bundle(RankingPlatform.淘宝);
  let releaseTaobao;
  globalThis.fetch = async (url) => {
    if (url.includes("/taobao/") && url.endsWith("latest.json")) return { ok: true, json: () => new Promise((resolve) => { releaseTaobao = resolve; }) };
    return fixtureFetch(compass, taobao)(url);
  };
  try {
    await act(async () => { environment.root.render(createElement(RankingApp, { dataIndexUrl: "https://example.invalid/compass/latest.json", taobaoDataIndexUrl: "https://example.invalid/taobao/latest.json" })); await new Promise((resolve) => setImmediate(resolve)); });
    await click(environment.dom, "淘宝");
    assert.ok(environment.dom.window.document.body.textContent.includes("正在加载最新榜单"));
    assert.equal(environment.dom.window.document.querySelectorAll(".platform-switch button").length, 2);
    await click(environment.dom, "抖音");
    await act(async () => { releaseTaobao(taobao.latest); await new Promise((resolve) => setImmediate(resolve)); });
    assert.ok(environment.dom.window.document.body.textContent.includes("compass合成商品1"));
    assert.ok(!environment.dom.window.document.body.textContent.includes("taobao合成商品1"));
    globalThis.fetch = async (url) => url.includes("/taobao/") ? { ok: false } : fixtureFetch(compass, taobao)(url);
    await click(environment.dom, "淘宝");
    assert.ok(environment.dom.window.document.body.textContent.includes("暂时无法读取最新榜单"));
    await click(environment.dom, "抖音");
    assert.ok(environment.dom.window.document.body.textContent.includes("compass合成商品1"));
  } finally { globalThis.fetch = originalFetch; await environment.cleanup(); }
});

test("空淘宝快照保持筛选与平台切换而不残留抖音商品", async () => {
  // 合法空榜保留元信息，不能被当成网络错误或填充上一平台记录。
  const environment = setup();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fixtureFetch(bundle(RankingPlatform.抖音), bundle(RankingPlatform.淘宝, 0));
  try {
    await act(async () => { environment.root.render(createElement(RankingApp, { dataIndexUrl: "https://example.invalid/compass/latest.json", taobaoDataIndexUrl: "https://example.invalid/taobao/latest.json" })); await new Promise((resolve) => setImmediate(resolve)); });
    await click(environment.dom, "淘宝");
    assert.ok(environment.dom.window.document.body.textContent.includes("没有符合当前筛选条件"));
    assert.ok(!environment.dom.window.document.body.textContent.includes("compass合成商品"));
    assert.equal(environment.dom.window.document.querySelectorAll(".platform-switch button").length, 2);
  } finally { globalThis.fetch = originalFetch; await environment.cleanup(); }
});
