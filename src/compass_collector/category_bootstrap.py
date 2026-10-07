"""从实际 YAML 和本地原始分类目录导入待核对规则，不采集商品。"""

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from compass_collector.app_paths import runtime_root
from compass_collector.category_catalog import catalog_from_payload, scope_path
from compass_collector.category_rules import contains, validate_catalog
from compass_collector.platform_runtime import PlatformRuntime
from compass_collector.platforms.registry import create_adapter
from compass_collector.errors import CollectorError
from compass_collector.remote_categories import remote_request, upload_catalog


def compact_paths(paths):
    """父路径覆盖的子路径不重复保存，保留首次出现的源顺序。"""
    # 草稿规范化与网页父级选择使用相同语义。
    result = []
    for path in paths:
        if any(contains(parent, path) for parent in result):
            continue
        result = [previous for previous in result if not contains(path, previous)]
        result.append(path)
    return result


def legacy_rules(config, nodes, discovery):
    """把旧名称命中展开为精确排除节点，保留未匹配名称供网页处理。"""
    # 配置已经按实际入口平台过滤，不合并其他 YAML 文件。
    platform = config.execution_platform()
    # 全部任务都登记，启用状态和调度仍留在 YAML。
    tasks = []
    for task in config.tasks:
        # 旧目标映射失败必须报告，不能隐式导入整个行业。
        scope = task.category_scope
        if scope.mode == "selected":
            # 先用原平台验证器确认旧配置能解析，再转成完整路径。
            if platform == "compass":
                from compass_collector.platforms.compass import select_scopes
                selected = select_scopes(discovery, scope).categories
            else:
                # 完整目录下按三级 ID 和原根范围核对旧目标。
                selected = [item for item in discovery.categories if item.category_id in scope.targets and item.level1_category_id == scope.root_category_id]
                if {item.category_id for item in selected} != set(scope.targets):
                    raise ValueError("旧淘宝目标分类缺失")
            targets = [scope_path(item) for item in selected]
        elif platform == "compass":
            targets = [node["path"] for node in nodes if len(node["path"]) == 1 and (scope.industry_id is None or node["path"][0] == scope.industry_id)]
        else:
            targets = [[scope.root_category_id], *[[root.root_category_id, branch] for root in scope.additional_roots for branch in root.level2_category_ids]]
        if not targets:
            raise ValueError("旧配置没有可导入的目标范围")
        tasks.append({"id": task.id, "display_name": task.display_name, "targets": compact_paths(targets)})
    # 命中来源独立展示，不能只显示合并后黑名单让用户丢失旧名称含义。
    exclusions, unresolved, matches = [], [], []
    for name in config.platforms[platform].category_blacklist:
        # 名称精确匹配所有路径，包括当前范围外的同名分类。
        matched = [node for node in nodes if node["name"] == name]
        if not matched:
            unresolved.append(name)
        for node in matched:
            matches.append({"name": name, "path": node["path"]})
            # 第一版黑名单不允许一级，旧一级排除展开成直属二级。
            exclusions.extend([child["path"] for child in nodes if len(child["path"]) == 2 and contains(node["path"], child["path"])] if len(node["path"]) == 1 else [node["path"]])
    return {"tasks": tasks, "exclusions": compact_paths(exclusions), "unresolved_names": unresolved}, matches


def initialize_categories(config, *, catalog_path=None, refresh=False, directory_only=False):
    """先同步目录，再导入草稿；网页发布之前不能启用 remote 采集。"""
    # 目录查找只在当前平台目录内进行，不碰其他平台材料。
    platform, root = config.execution_platform(), runtime_root()
    # 快照时刻取原材料时间，防止旧材料导入覆盖已更新目录。
    captured_at = datetime.now(timezone.utc)
    if refresh:
        # 仅请求分类响应，与采集/登录共用执行锁，不创建商品批次。
        with PlatformRuntime(root, platform).operation("collection", config.browser_for(platform).profile_dir):
            # 适配器资源在 finally 中释放，不留下占用 Profile 的 Chrome。
            adapter = create_adapter(platform, config.browser_for(platform), config.collection, manual=True)
            try:
                payload = adapter.discover_scopes(config.tasks[0], full_catalog=True).payload
                nodes, discovery = catalog_from_payload(platform, payload)
                # 独立目录刷新也保存原始响应，后续导入无需重复打开浏览器。
                source_directory = root / "category-catalogs" / platform
                source_directory.mkdir(parents=True, exist_ok=True)
                (source_directory / "source.json.gz").write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode()))
            finally:
                adapter.close()
    else:
        # 无参数时按平台寻找最近有效原树；不借助商品榜单反推目录。
        # 独立目录刷新材料与采集批次材料使用同一来源优先级。
        cached = root / "category-catalogs" / platform / "source.json.gz"
        candidates = [Path(catalog_path)] if catalog_path else sorted(
            [*((root / "raw" / platform).glob("*/*/*/category-tree.json.gz")), *([cached] if cached.is_file() else [])],
            key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates:
            raise ValueError("没有本地原始目录，请使用 --refresh 仅获取分类目录")
        # 无效历史文件允许继续找下一份；显式路径不静默换来源。
        for path in candidates:
            try:
                payload = json.loads(gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_text(encoding="utf-8"))
                nodes, discovery = catalog_from_payload(platform, payload)
                captured_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                break
            except (ValueError, OSError, CollectorError) as error:
                if catalog_path or path == candidates[-1]:
                    raise ValueError("本地分类目录无效，请使用 --refresh") from error
        validate_catalog(nodes)
    if not upload_catalog(platform, nodes, captured_at, root):
        raise ValueError("目录上传失败，本地已保留待同步材料")
    if directory_only:
        return {"directory_synced": True, "node_count": len(nodes)}
    # 初始化只保存草稿和命中说明，不悄悄改变当前生效规则。
    rules, matches = legacy_rules(config, nodes, discovery)
    return remote_request(platform, "/bootstrap", {"rules": rules, "migration_matches": matches})
