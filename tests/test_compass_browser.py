"""Exercise real Chrome interaction against fully intercepted controlled pages."""

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
import pytest
from compass_collector.browser import open_browser as real_open_browser
from compass_collector.config import load_config, CategoryScopeConfig
from compass_collector.platforms.compass import CompassAdapter, RANK_PATH
from compass_collector.platforms.compass_product_rank import build_request_params
from compass_collector.platforms.compass_categories import (
    parse_category_tree,
    CATEGORY_TREE_ENDPOINT_PATH,
)
from compass_collector.run_control import CollectionControl
from compass_collector.errors import CollectionInterruptedError, HttpRequestError
from test_category_collection import build_page_payload

# Desktop launches are opt-in; all network traffic is locally intercepted.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1",
    reason="Requires a local visible Google Chrome session",
)


def test_real_click_scroll_custom_price_retry_and_cancel(tmp_path, monkeypatch):
    """Use real locator/mouse actions, local response listeners, retry and stop."""
    config = load_config(Path("config/tasks.yaml"))
    task = config.tasks[0].model_copy(update={"category_scope": CategoryScopeConfig()})
    tree = json.loads(Path("tests/fixtures/category_tree.json").read_text())
    scope = parse_category_tree(tree).categories[0]
    business_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # Browser JavaScript emits safe business queries exactly as the real site does.
    params = build_request_params(task, scope, business_date, 1)
    html = """<html><body>
<button onclick="init()">商品榜单</button><button onclick="query()">实时</button>
<span class="aurora-select-content-value" onclick="document.getElementById('menu').hidden=false">PATH</span>
<div id="menu" hidden>MENU<button onclick="select(false)">全部</button></div>
<div><button onclick="brand(-1)">不限</button><button>知名品牌</button><button onclick="brand(0)">非知名品牌</button></div>
<div class="tagList-fixture"><button onclick="price('不限')">不限</button><button onclick="document.getElementById('price').showModal()">自定义</button></div>
<dialog id="price" role="dialog"><input placeholder="输入最小值" id="min"><button onclick="apply()">确定</button></dialog>
<div style="height:1500px">Products</div>
<ul class="aurora-pagination"><li class="aurora-pagination-item-active">1</li><li class="aurora-pagination-next" aria-disabled="false" title="下一页"><button onclick="next()">Next</button></li></ul>
<script>
let params=PARAMS;
function init(){fetch('CATEGORY');}
function query(){fetch('RANK?'+new URLSearchParams(params));}
function brand(v){params.brand_type=v;params.page_no=1;query();}
function price(v){params.price_bin=v;params.page_no=1;query();}
function apply(){document.getElementById('price').close();price(document.getElementById('min').value+'-?');}
function select(valid){document.getElementById('menu').hidden=true;params.category_id=valid?'CASCADE':'0';params.price_bin='不限';params.page_no=1;document.querySelector('.aurora-pagination-item-active').textContent='1';query();}
function next(){if(window.scrollY<900)throw Error('must scroll');params.page_no++;document.querySelector('.aurora-pagination-item-active').textContent=params.page_no;query();}
</script></body></html>"""
    # Flat menu faithfully exposes the three observed category names.
    menu = "".join(
        f'<button onclick="{("select(true)" if index == 2 else "void(0)")}">{name}</button>'
        for index, name in enumerate(scope.path)
    )
    html = (
        html.replace("PARAMS", json.dumps(params))
        .replace("CATEGORY", CATEGORY_TREE_ENDPOINT_PATH)
        .replace("RANK", RANK_PATH)
        .replace("CASCADE", params["category_id"])
        .replace("MENU", menu)
        .replace("PATH", " / ".join(scope.path))
    )
    requests = []
    attempt_two = 0
    # 第二次第二页模拟登录失效，恢复后仅交付未完成页。
    auth_failure = 0
    # 分类首次发现也模拟认证错误，验证真实页面重新进入后恢复。
    discovery_auth_failure = 0

    def open_controlled(browser_config):
        """Install routes before the adapter navigates or creates its popup."""
        session = real_open_browser(browser_config)

        def route_handler(route):
            """Serve all pages and data locally, including one missing page response."""
            nonlocal attempt_two, auth_failure, discovery_auth_failure
            parts = urlsplit(route.request.url)
            if parts.path == CATEGORY_TREE_ENDPOINT_PATH:
                if discovery_auth_failure == 0:
                    discovery_auth_failure += 1
                    route.fulfill(json={"st": 10012, "data": {}})
                else:
                    route.fulfill(json=tree)
            elif parts.path == RANK_PATH:
                query = parse_qs(parts.query, keep_blank_values=True)
                page_no = int(query["page_no"][0])
                requests.append(query)
                if page_no == 2 and attempt_two == 0:
                    attempt_two += 1
                    route.abort()
                elif page_no == 2 and auth_failure == 0:
                    auth_failure += 1
                    route.fulfill(json={"st": 10012, "data": {}})
                else:
                    route.fulfill(
                        json=build_page_payload(
                            category_id=scope.category_id, page_no=page_no, total=13
                        )
                    )
            elif parts.path == "/shop":
                route.fulfill(
                    body="<button onclick=\"window.open('/shop/chance/rank-shop')\">查看同行榜单</button>",
                    content_type="text/html; charset=utf-8",
                )
            elif parts.path == "/shop/chance/rank-shop":
                route.fulfill(body=html, content_type="text/html; charset=utf-8")
            else:
                route.fulfill(body="", status=404)

        session.context.route("**/*", route_handler)
        return session

    monkeypatch.setattr(
        "compass_collector.platforms.compass.open_browser", open_controlled
    )
    settings = config.collection.model_copy(
        update={
            "response_timeout_seconds": 0.4,
            "action_timeout_seconds": 2,
            "manual_auth_wait_seconds": 0.1,
            "request_interval_seconds": config.collection.request_interval_seconds.model_copy(
                update={"min": 0.01, "max": 0.01}
            ),
        }
    )
    adapter = CompassAdapter(
        config.browser_for("compass").model_copy(
            update={"profile_dir": tmp_path / "profile"}
        ),
        settings,
        manual=True,
    )
    try:
        discovery = adapter.discover_scopes(task)
        assert discovery_auth_failure == 1
        pages = list(
            adapter.collect_scope(
                task, discovery.discovery.categories[0], business_date
            )
        )
        assert [page.page_no for page in pages] == [1, 2]
        assert sum(len(page.entries) for page in pages) == 13
        assert attempt_two == 1
        assert auth_failure == 1
        custom = task.model_copy(
            update={"filters": task.filters.model_copy(update={"price_bin": "10001-?"})}
        )
        custom_pages = list(
            adapter.collect_scope(
                custom, discovery.discovery.categories[0], business_date
            )
        )
        assert all(page.safe_params["price_bin"] == "10001-?" for page in custom_pages)
        control = CollectionControl()
        adapter.control = control
        iterator = adapter.collect_scope(
            task, discovery.discovery.categories[0], business_date
        )
        assert next(iterator).page_no == 1
        control.request_stop()
        with pytest.raises(CollectionInterruptedError):
            next(iterator)
    finally:
        adapter.close()
    assert any(query["page_no"] == ["2"] for query in requests)


def test_pagination_edge_interception_and_bounded_obstruction(tmp_path):
    """Real mouse hits exposed button areas across scopes without repeated scrolling."""
    # 独立 Profile 与本地 HTML 避免访问平台或影响用户的登录态。
    config = load_config(Path("config/tasks.yaml"))
    session = real_open_browser(
        config.browser_for("compass").model_copy(
            update={"profile_dir": tmp_path / "edge-profile"}
        )
    )
    # 用短超时验证完全遮挡时安全结束，避免无限动作重试。
    adapter = CompassAdapter(
        config.browser_for("compass"),
        config.collection.model_copy(
            update={"action_timeout_seconds": 0.2, "scroll_settle_seconds": 0.1}
        ),
        manual=True,
    )
    adapter.page = session.page
    try:
        session.page.set_content("""<style>
            body {margin:0; height:1800px}
            .aurora-pagination-next button {position:fixed; right:0; top:400px; width:24px; height:24px}
            #overlay {position:fixed; right:0; top:0; bottom:0; width:15px; z-index:10}
            </style><ul class="aurora-pagination">
            <li class="aurora-pagination-next" aria-disabled="false"><button onclick="window.clicks++">Next</button></li>
            </ul><div id="overlay"></div><script>window.clicks=0;</script>""")
        # 跨分类首页会重复出现同一右边缘遮挡，必须每次只产生一个有效点击。
        for expected_clicks in range(1, 4):
            adapter._next_page()
            assert session.page.evaluate("window.clicks") == expected_clicks
            assert session.page.evaluate("window.scrollY") == 0
        session.page.evaluate("document.querySelector('#overlay').style.width='30px'")
        with pytest.raises(HttpRequestError) as error:
            adapter._next_page()
        assert error.value.category == "pagination_obstructed"
        assert session.page.evaluate("window.clicks") == 3
    finally:
        session.close()
