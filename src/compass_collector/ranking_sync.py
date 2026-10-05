"""从采集 PostgreSQL 同步正式榜单到业务 API，失败保留可重试材料。"""

import gzip
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import create_engine, text


def build_sync_payload(index: dict) -> dict:
    """只读提取正式发布批次，按既有 CSV 顺序关联数据库商品行。"""
    # 每个平台继续使用独立采集库，不复用选品业务库连接。
    variable = {"compass": "COMPASS_DATABASE_URL", "taobao": "TAOBAO_DATABASE_URL"}[index["platform"]]
    # 连接只在一次同步中存活，数据库密码不写入结果或日志。
    engine = create_engine(os.environ[variable], pool_pre_ping=True, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as connection:
            # 数据库只读事务确保同步逻辑不会意外修改采集状态。
            connection.execute(text("SET TRANSACTION READ ONLY"))
            # 已发布批次是同步依据，不凭 runtime 暂存文件判断采集成功。
            batch = connection.execute(text("""SELECT platform, task_id, business_date, published_at
                FROM collection_batches WHERE id = :batch AND published_at IS NOT NULL"""),
                {"batch": index["batch_id"]}).mappings().one()
            if (batch["platform"] != index["platform"] or batch["task_id"] != index["task_id"]
                    or batch["business_date"].isoformat() != index["business_date"]):
                raise ValueError("ranking_sync_batch_mismatch")
            # 排序与 attach_selection_identity 完全一致；同商品重复上榜仍保留每行。
            result = connection.execute(text("""SELECT p.*, c.level1_category_name, c.level2_category_name,
                c.category_name FROM product_rank_entries p JOIN category_runs c ON c.id = p.category_run_id
                WHERE c.batch_id = :batch ORDER BY c.discovery_order, p.rank"""),
                {"batch": index["batch_id"]}).mappings()
            # 数值边界按十进制文本传输，避免 JSON 浮点损失精度。
            rows = []
            for row in result:
                # 采集库墙上时间遵循项目既有北京时间语义。
                captured_at = row["captured_at"]
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                # 只同步指标列，任何连接、原始响应或浏览器状态都不会进入业务 API。
                metrics = {key: str(value) if value is not None else None for key, value in row.items()
                           if key.endswith(("_min_value", "_max_value", "_unit", "_raw"))}
                rows.append({"collector_entry_id": row["id"], "category_run_id": row["category_run_id"],
                    "captured_at": captured_at.isoformat(), "product_id": row["product_id"], "rank": row["rank"],
                    "product_name": row["product_name"],
                    "category": " > ".join([row["level1_category_name"], row["level2_category_name"], row["category_name"]]),
                    "metrics": metrics})
            if len(rows) != index["item_count"]:
                raise ValueError("ranking_sync_count_mismatch")
            return {"index": index, "rows": rows}
    finally:
        engine.dispose()


def sync_published_index(index_path: Path) -> dict | None:
    """同步成功写回执；网络失败抛出可重试异常，不更改正式采集和网页发布结果。"""
    # 两项均未配置时保持现有离线行为；仅配置一项视为错误，避免静默漏同步。
    api_url, token = os.environ.get("RANKING_API_URL", ""), os.environ.get("RANKING_SYNC_TOKEN", "")
    if not api_url and not token:
        return None
    if not api_url or not token:
        raise ValueError("ranking_sync_config_incomplete")
    # 只允许 HTTPS 服务或本机验收地址，不向任意明文公网发送凭证。
    parsed = httpx.URL(api_url)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.host in {"127.0.0.1", "localhost"})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ranking_sync_url_invalid")
    # 本地发布索引只用于定位已核验的数据库批次。
    index = json.loads(index_path.read_text(encoding="utf-8"))
    payload = build_sync_payload(index)
    # 大榜单压缩上传；服务端分别限制压缩和解压后的体积。
    body = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        # Bearer 密钥仅存在请求头，不进入日志或快照文件。
        response = client.post(api_url.rstrip("/") + "/api/rankings/sync", content=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Content-Encoding": "gzip"})
        if response.status_code != 200:
            raise ValueError(f"ranking_sync_http_{response.status_code}")
        result = response.json()
    # 回执不含密钥，供离线补同步和运维核对。
    (index_path.parent / "ranking-sync.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
