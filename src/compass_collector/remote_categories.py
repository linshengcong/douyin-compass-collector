"""远程类目规则读取和目录上报；失败不能静默回退到 YAML。"""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from compass_collector.category_rules import validate_rules, validate_catalog
from compass_collector.errors import CollectorError


def remote_connection():
    """复用现有榜单 API 和同步令牌，日志与快照不包含令牌。"""
    # 地址只允许 HTTPS 或本机验收 HTTP，禁止跟随重定向发送密钥。
    api_url, token = os.environ.get("RANKING_API_URL", ""), os.environ.get("RANKING_SYNC_TOKEN", "")
    if not api_url or not token:
        raise CollectorError("远程分类配置缺少 API 地址或同步令牌", category="category_remote_connection_missing")
    # URL 元数据校验与现有榜单同步保持一致。
    try:
        parsed = httpx.URL(api_url)
    except httpx.InvalidURL as error:
        raise CollectorError("远程分类 API 地址无效", category="category_remote_url_invalid") from error
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.host in {"127.0.0.1", "localhost"})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CollectorError("远程分类 API 地址无效", category="category_remote_url_invalid")
    return api_url.rstrip("/"), {"Authorization": "Bearer " + token}


def remote_request(platform, suffix="", body=None):
    """固定端点读取或写入，网络与 HTTP 异常转换为安全错误。"""
    # 连接仅在需要新批次或初始化时读取，补录不依赖网络。
    base, headers = remote_connection()
    try:
        with httpx.Client(timeout=35, follow_redirects=False) as client:
            # 平台来自已经校验的任务，不接受任意路径。
            url = f"{base}/api/collection-categories/{platform}{suffix}"
            response = client.get(url, headers=headers) if body is None else client.post(url, json=body, headers=headers)
            if response.status_code != 200:
                raise CollectorError(f"分类接口请求失败（HTTP {response.status_code}）", category="category_remote_http_error")
            return response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise CollectorError("分类接口不可用或响应无效", category="category_remote_unavailable") from error


def fetch_category_config(platform, task_id):
    """仅采用已发布兼容版本；未初始化和未核对草稿均停止新采集。"""
    # 服务端一次返回共享黑名单和全部任务范围，保证同版本。
    payload = remote_request(platform)
    try:
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["platform"] != platform or not payload["published"] or type(payload["revision"]) is not int or payload["revision"] < 1:
            raise ValueError("规则未发布或版本不兼容")
        validate_rules(payload["rules"])
        if payload["rules"]["unresolved_names"] or not any(task["id"] == task_id for task in payload["rules"]["tasks"]):
            raise ValueError("规则任务缺失或名称未处理")
    except (ValueError, KeyError, TypeError) as error:
        raise CollectorError("分类规则未发布、不兼容或任务未注册", category="category_remote_invalid") from error
    return {"schema_version": 1, "platform": platform, "revision": payload["revision"], "rules": payload["rules"]}


def upload_catalog(platform, nodes, captured_at, runtime_root, logger=None):
    """先持久化待同步目录；失败保留文件，下次新批次重试。"""
    validate_catalog(nodes)
    # 平台独立目录不包含规则凭证，并用于首次导入。
    directory = Path(runtime_root) / "category-catalogs" / platform
    directory.mkdir(parents=True, exist_ok=True)
    # 完整目录在传输之前落盘，进程退出不会丢失待同步内容。
    pending = directory / "pending.json"
    # 待同步与最新目录具有相同安全契约。
    body = {"nodes": nodes, "captured_at": captured_at.isoformat()}
    # 同目录原子替换避免读到半个目录。
    temporary = directory / f"pending-{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    temporary.replace(pending)
    (directory / "latest.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return retry_catalog(platform, runtime_root, logger)


def retry_catalog(platform, runtime_root, logger=None):
    """重放最新待同步文件，不阻断已校验的新采集规则。"""
    # 文件仅保存安全分类节点；上传成功后才删除待同步标记。
    pending = Path(runtime_root) / "category-catalogs" / platform / "pending.json"
    if not pending.is_file():
        return True
    try:
        # 只确认本次读到的材料，避免同步期间新上传材料被错误删除。
        content = pending.read_text(encoding="utf-8")
        remote_request(platform, "/catalog", json.loads(content))
        if pending.is_file() and pending.read_text(encoding="utf-8") == content:
            pending.unlink()
        return True
    except (CollectorError, ValueError, OSError):
        if logger:
            logger.emit(level="WARNING", event="category_catalog_sync_pending",
                message="分类目录同步失败，已保留本地材料等待下次运行重试", stage="category_discovery")
        return False
