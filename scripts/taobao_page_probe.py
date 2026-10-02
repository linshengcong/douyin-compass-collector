"""Inspect the collector's own Taobao page without reading authentication storage."""

import gzip
import json
import argparse
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from compass_collector.config import load_config
from compass_collector.platforms.registry import create_adapter
from compass_collector.platforms.taobao_capture import read_completed_payload
from compass_collector.platforms.taobao_categories import parse_category_tree


def main():
    """Save bounded page evidence from the production profile and navigation path."""
    # 仅检查项目独立 Profile；不连接用户日常浏览器或读取 Cookie/LocalStorage。
    # 本轮可显式选择已有采集 Profile 的临时配置，不修改默认账号目录。
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/taobao-poc.yaml"))
    parser.add_argument("--inspect-category", action="store_true")
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    adapter = create_adapter("taobao", config.browser_for("taobao"), config.collection, manual=True)
    output = Path("runtime/acceptance/taobao/2026-10-01/page-probe")
    output.mkdir(parents=True, exist_ok=True)
    # 诊断失败仍保存安全摘要；不把异常文本或完整 URL 带入材料。
    summary = {}
    discovery = None
    try:
        adapter.open_session(login_only=True)
        # 不按固定15秒关闭页面；复用生产认证等待及登录后首页衔接。
        initial_path = urlsplit(adapter.page.url).path
        adapter._authenticate()
        # 分类请求也按完整响应等待，避免可见分页已出现但 taxonomy 尚在加载。
        deadline = monotonic() + config.collection.response_timeout_seconds
        while adapter.capture.category_response is None and monotonic() < deadline:
            adapter._pump(0.1)
        # 仅记录来源和路径，完整 URL 可能包含认证参数，不能进入诊断摘要。
        parts = urlsplit(adapter.page.url)
        summary = {"origin": f"{parts.scheme}://{parts.hostname}", "path": parts.path,
                   "initial_path": initial_path,
                   "authenticated_control_check": adapter.controls.authenticated(adapter.page)}
        # 只读取结构类名和数量，不保存账号输入、网页脚本或完整 HTML。
        selectors = [".oui-page-size-select", ".ant-cascader-picker", ".ant-cascader-menus",
                     ".ant-pagination-next", ".ant-pagination-item-active", "[role=combobox]"]
        summary["counts"] = {selector: adapter.page.locator(selector).count() for selector in selectors}
        summary["category_classes"] = adapter.page.locator("[class*='cate']").evaluate_all(
            "nodes => [...new Set(nodes.map(node => String(node.className)))].slice(0,40)")
        # 分类接口只读取当前页面已经完成的响应，不重放认证 HTTP 请求。
        if adapter.capture.category_response is not None:
            payload = read_completed_payload(adapter.capture.category_response)
            with gzip.open(output / "category-tree.json.gz", "wt", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            discovery = parse_category_tree(payload, config.tasks[0].category_scope)
            summary["target_categories"] = [{"id": scope.key, "path": scope.path}
                                             for scope in discovery.categories]
        else:
            summary["category_response_completed"] = False
        if arguments.inspect_category and summary["authenticated_control_check"] and discovery is not None:
            # 根据本轮真实 DOM 打开类目控件，只检查结构，不选分类或提交筛选。
            adapter.page.locator(".item-cate").click(timeout=3000)
            adapter._pump(1)
            # 真实二级名称来自当次分类树，仅悬停检查三级菜单是否展开。
            parent_name = discovery.categories[0].path[1]
            adapter.page.locator(".common-picker-menu").get_by_text(parent_name, exact=True).hover(timeout=3000)
            adapter._pump(0.5)
            summary["category_popup"] = adapter.page.locator(".common-picker-menu [class]").evaluate_all(
                """nodes => nodes.filter(node => node.getBoundingClientRect().width && node.getBoundingClientRect().height)
                    .slice(0,60).map(node => ({tag:node.tagName, className:String(node.className),
                        role:node.getAttribute('role'), text:(node.innerText||'').slice(0,150)}))""")
            # 按列读取分类名称，避免三级大量节点截断一级列的结构证据。
            summary["category_columns"] = adapter.page.locator(".common-picker-menu ul.tree-menu").evaluate_all(
                """nodes => nodes.map(node => ({className:node.className,
                    items:[...node.querySelectorAll(':scope > li')].map(item => ({
                        text:item.innerText, className:item.className}))}))""")
    except Exception as error:
        # 保存错误种类即可，不记录可能包含认证内容的消息或堆栈。
        summary["error"] = {"type": type(error).__name__, "category": getattr(error, "category", None)}
        if adapter.page is not None:
            # 失败也保留最终页面路径与控件状态，区分登录跳转和其他加载失败。
            parts = urlsplit(adapter.page.url)
            summary.update({"origin": f"{parts.scheme}://{parts.hostname}", "path": parts.path,
                            "authenticated_control_check": adapter.controls.authenticated(adapter.page)})
    finally:
        try:
            if adapter.page is not None:
                # 截图仅保存在本地验收目录，不公开发布；截图失败不吞掉原始诊断。
                try:
                    adapter.page.screenshot(path=str(output / "page.png"), timeout=10000)
                except Exception as error:
                    summary["screenshot_error_type"] = type(error).__name__
        finally:
            try:
                adapter.close()
            except Exception as error:
                summary["close_error_type"] = type(error).__name__
            (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if "error" in summary or "close_error_type" in summary else (
        0 if summary.get("authenticated_control_check") else 2)


if __name__ == "__main__":
    raise SystemExit(main())
