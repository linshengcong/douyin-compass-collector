"""Validate page-generated Taobao live responses and preserve source metrics."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from compass_collector.errors import ResponseContractError
from compass_collector.models import MetricRange, ProductRankEntry, ProductShop

# 固定路径和页大小是已确认的实时契约，不包含认证资料。
RANK_PATH = "/mc/mq/mkt/item/live/rank.json"
PAGE_SIZE = 20
# 单值和区间分别支持原文中的中文量级；不接受科学计数或负值。
VALUE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)(万|亿)?$")


@dataclass(frozen=True, slots=True)
class TaobaoPageContract:
    """Return the complete paging plan without requiring a response page field."""

    # 页数由第一页的真实总数和固定二十条计算。
    api_total: int
    target_page_count: int


def build_expected_params(task, scope, business_date: date, page_no: int) -> dict:
    """Describe only the safe business query expected from UI operations."""
    if type(page_no) is not int or page_no < 1:
        raise ValueError("page_no must be a positive integer")
    # 日期在任务边界计算；签名与随机时间戳不属于匹配契约。
    day = business_date.isoformat()
    return {
        "dateRange": f"{day}|{day}", "dateType": task.date.date_type,
        "pageSize": PAGE_SIZE, "page": page_no,
        "cateId": scope.platform_metadata["cate_id"],
        "cateFlag": scope.platform_metadata["cate_flag"],
        "rankType": task.rank.rank_type, "minPrice": task.filters.min_price,
        "maxPrice": task.filters.max_price, "priceSeg": task.filters.price_segment,
        "sellerType": task.filters.seller_type, "keyWord": task.filters.keyword,
        "indexCode": "payByrCnt,uv", "marketVersion": "free",
    }


def matches_rank_request(request, expected: dict) -> bool:
    """Reject wrong origin, mode, dates, pages and duplicate business parameters."""
    try:
        # 只在内存查看 URL；调用方仅保存 expected 的白名单参数。
        parts = urlsplit(request.url)
        if parts.scheme != "https" or parts.hostname != "sycm.taobao.com" or parts.port not in (None, 443) or parts.path != RANK_PATH or parts.username is not None or parts.password is not None:
            return False
        # 保留空筛选，并要求业务键恰好只有一个值。
        query = parse_qs(parts.query, keep_blank_values=True)
        return all(query.get(key) == [str(value)] for key, value in expected.items())
    except (AttributeError, ValueError):
        return False


def _ranking_data(payload: dict) -> dict:
    """Read the observed nested envelope with strict successful status."""
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        raise ResponseContractError("Taobao ranking response failed", category="taobao_business_error")
    # 两层 data 分别是实时元信息和排行容器。
    outer = payload.get("data")
    data = outer.get("data") if isinstance(outer, dict) else None
    # 真实空榜也会返回 code=0、data.data=[]；仅此明确空列表等价于零条。
    if isinstance(data, list) and not data:
        return {"recordCount": 0, "data": []}
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ResponseContractError("Invalid Taobao ranking envelope", category="invalid_page_result")
    return data


def validate_page_payload(payload: dict, *, requested_page: int, expected_total: int | None = None) -> TaobaoPageContract:
    """Require all twenty-row pages and the exact final remainder."""
    if type(requested_page) is not int or requested_page < 1:
        raise ValueError("requested_page must be positive")
    if expected_total is not None and (type(expected_total) is not int or expected_total < 0):
        raise ValueError("expected_total must be a non-negative integer")
    # 总数来自排行容器，而不是外层更新时间。
    data = _ranking_data(payload)
    total = data.get("recordCount")
    if type(total) is not int or total < 0:
        raise ResponseContractError("Invalid Taobao record count", category="invalid_page_result")
    if expected_total is not None and total != expected_total:
        raise ResponseContractError("Taobao total changed", category="inconsistent_total")
    # 0 条也只验证第一页，非空尾页不能多取或少取。
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    count = max(0, min(PAGE_SIZE, total - (requested_page - 1) * PAGE_SIZE))
    if requested_page > pages or len(data["data"]) != count:
        raise ResponseContractError("Taobao page is incomplete", category="incomplete_page")
    return TaobaoPageContract(total, pages)


def parse_metric_value(raw) -> tuple[str | None, MetricRange | None]:
    """Preserve the original count text alongside optional actual-unit bounds."""
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not raw.strip():
        raise ResponseContractError("Invalid Taobao metric", category="invalid_metric")
    if raw.strip() == "-":
        return raw, None
    # 两端各自解释量级，避免把万误作原始人数。
    segments = [part.strip() for part in raw.split("~")]
    if len(segments) not in (1, 2):
        raise ResponseContractError("Invalid Taobao metric range", category="invalid_metric")
    values = []
    for segment in segments:
        # 精确正则拒绝负数、尾随垃圾和非法量级。
        match = VALUE_PATTERN.fullmatch(segment)
        if match is None:
            raise ResponseContractError("Invalid Taobao metric value", category="invalid_metric")
        multiplier = {"万": 10_000, "亿": 100_000_000}.get(match[2], 1)
        values.append(Decimal(match[1]) * multiplier)
    if len(values) == 1:
        values.append(values[0])
    if values[0] > values[1] or any(value != value.to_integral_value() or value >= Decimal("1e20") for value in values):
        raise ResponseContractError("Invalid Taobao count bounds", category="invalid_metric")
    return raw, MetricRange(values[0], values[1], "count")


def normalize_url(value, *, required: bool = False) -> str | None:
    """Normalize source links and reject executable or credential-bearing URLs."""
    if value is None or value == "":
        if not required:
            return None
        raise ResponseContractError("Taobao product URL is missing", category="invalid_product")
    if not isinstance(value, str):
        raise ResponseContractError("Invalid Taobao URL", category="invalid_product")
    # 协议相对链接补齐协议，其他 HTTP(S) 链接保留原文。
    normalized = "https:" + value if value.startswith("//") else value
    try:
        # 非法 IPv6 或端口也应转成统一契约错误，不能泄漏原始 URL。
        parts = urlsplit(normalized)
        parts.port
    except ValueError as error:
        raise ResponseContractError("Invalid Taobao URL", category="invalid_product") from error
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ResponseContractError("Unsafe Taobao URL", category="invalid_product")
    return normalized


def _value(row: dict, field: str):
    """Require the observed metric wrapper while allowing its null value."""
    # value 键缺失表示契约错误，不能伪装为缺失指标。
    wrapped = row.get(field)
    if not isinstance(wrapped, dict) or "value" not in wrapped:
        raise ResponseContractError("Missing Taobao field wrapper", category="invalid_product")
    return wrapped["value"]


def parse_page_entries(payload: dict, *, page_no: int, captured_at: datetime) -> list[ProductRankEntry]:
    """Keep product identity, rank, links, seller identity and original counts."""
    if type(page_no) is not int or page_no < 1:
        raise ValueError("page_no must be a positive integer")
    # 解析与分页校验独立，调用方应先检查整页条数。
    rows = _ranking_data(payload)["data"]
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise ResponseContractError("Invalid Taobao product row", category="invalid_product")
        # 商品和店铺结构是已观察到的普通对象，不是 value 包装。
        item, shop = row.get("item"), row.get("shop")
        if not isinstance(item, dict) or not isinstance(shop, dict):
            raise ResponseContractError("Invalid Taobao item or shop", category="invalid_product")
        # 真实淘宝响应可省略店名；不能因此丢弃完整商品和卖家身份。
        identity, title, shop_name = item.get("itemId"), item.get("title"), shop.get("title")
        if not isinstance(identity, str) or not identity or not isinstance(title, str) or not title.strip():
            raise ResponseContractError("Invalid Taobao product identity", category="invalid_product")
        if shop_name is not None and not isinstance(shop_name, str):
            raise ResponseContractError("Invalid Taobao shop title", category="invalid_product")
        # 缺失、null、空文本或纯空白按用户约定统一显示为“未知”。
        if shop_name is None or not shop_name.strip():
            shop_name = "未知"
        if "itemId" in row and str(_value(row, "itemId")) != identity:
            raise ResponseContractError("Taobao item IDs disagree", category="invalid_product")
        # 原始排名是整数；前三级图标的空 DOM 不能代替它。
        rank = _value(row, "cateRankId")
        if type(rank) is not int or rank < 1:
            raise ResponseContractError("Invalid Taobao rank", category="invalid_product")
        buyers_raw, buyers = parse_metric_value(_value(row, "payByrCnt"))
        visitors_raw, visitors = parse_metric_value(_value(row, "uv"))
        # userId 是卖家用户标识，不赋值给未知店铺 ID。
        seller_id = shop.get("userId")
        if seller_id is not None and (type(seller_id) not in (str, int) or not str(seller_id).isdecimal()):
            raise ResponseContractError("Invalid Taobao seller ID", category="invalid_product")
        entries.append(ProductRankEntry(
            page_no=page_no, captured_at=captured_at, rank=rank,
            product_id=identity, product_name=title, newly_on_ranking=None,
            pay_amount=None, pay_combo_count=None,
            shops=(ProductShop(0, None, shop_name, normalize_url(shop.get("shopUrl")),
                               str(seller_id) if seller_id is not None else None),),
            image_url=normalize_url(item.get("pictUrl")),
            product_url=normalize_url(item.get("detailUrl"), required=True),
            pay_buyer_count_raw=buyers_raw, pay_buyer_count=buyers,
            visitor_count_raw=visitors_raw, visitor_count=visitors,
        ))
    # 页内顺序和排名范围共同验证实际请求页码。
    expected_ranks = list(range((page_no - 1) * PAGE_SIZE + 1, (page_no - 1) * PAGE_SIZE + len(entries) + 1))
    if [entry.rank for entry in entries] != expected_ranks:
        raise ResponseContractError("Taobao page ranks do not match request", category="invalid_ranking_sequence")
    return entries


def validate_complete_ranking(entries: list[ProductRankEntry], *, api_total: int) -> None:
    """Keep repeated item IDs while requiring complete, distinct source ranks."""
    if type(api_total) is not int or api_total < 0:
        raise ValueError("api_total must be non-negative")
    if len(entries) != api_total:
        raise ResponseContractError("Incomplete Taobao ranking", category="incomplete_ranking")
    # 实时翻页可重复出现同一商品，按源排名保留每条记录，不以商品ID判失败。
    ranks = {entry.rank for entry in entries}
    if len(ranks) != api_total:
        raise ResponseContractError("Duplicate Taobao rank", category="duplicate_rank")
    if ranks != set(range(1, api_total + 1)):
        raise ResponseContractError("Discontinuous Taobao ranking", category="invalid_ranking_sequence")
