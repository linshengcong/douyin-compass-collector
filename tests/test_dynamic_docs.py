"""Current dynamic-category documentation contract tests."""

import re
from pathlib import Path

from current_contract import CURRENT_INTERVAL_LABEL, CURRENT_PRICE_BIN_DOC_MARKER


# CURRENT_DOCUMENTS 是对外描述当前工程行为的文档集合。
CURRENT_DOCUMENTS = (
    Path("README.md"),
    Path("docs/工程方案.md"),
    Path("docs/故障处理.md"),
    Path("docs/备份与恢复.md"),
)


def test_documents_use_only_the_current_dynamic_category_contract() -> None:
    """Reject fixed-category and obsolete persistence language in current docs."""

    # document_text 合并全部当前文档，避免单个文件残留旧版契约。
    document_text = "\n".join(
        document_path.read_text(encoding="utf-8")
        for document_path in CURRENT_DOCUMENTS
    )
    # required_markers 锁定动态分类、完整分页和失败阈值核心语义。
    required_markers = (
        "所有一级分类",
        "all_level1",
        "排除",
        "忽略四级",
        "每次任务请求一次分类树",
        "二级分类 ID 与三级分类 ID",
        "完整请求全部页",
        CURRENT_INTERVAL_LABEL,
        "非知名品牌",
        CURRENT_PRICE_BIN_DOC_MARKER,
        "partial_success",
        "第 3 个",
    )
    # obsolete_markers 只用于拒绝旧配置和旧数据模型叙述。
    obsolete_markers = (
        "水饮冲调",
        "max_items",
        "200 条",
        "200条",
        "collection_runs",
        "不写 SQLite",
        "最近成功 CSV",
        "6 列",
        "六列",
        "category_id=1000001823",
        "按名称定位唯一",
        "dynamic_descendants",
    )

    assert all(marker in document_text for marker in required_markers)
    assert all(marker not in document_text for marker in obsolete_markers)
    # standalone_run_id 只拒绝旧批次身份，不误伤当前 category_run_id。
    standalone_run_id = re.search(r"(?<!category_)run_id", document_text)
    assert standalone_run_id is None


def test_documents_define_dry_run_publication_and_csv_boundaries() -> None:
    """Keep audit-only dry-runs distinct from officially published CSV data."""

    # readme 是日常运行入口的首要用户契约。
    readme = Path("README.md").read_text(encoding="utf-8")
    # design 是持久化与发布语义的详细工程契约。
    design = Path("docs/工程方案.md").read_text(encoding="utf-8")

    assert "collection_batches" in readme
    assert "category_runs" in readme
    assert "raw_responses" in readme
    assert "published_at" in readme
    assert "runtime/exports/<YYYY-MM-DD>/<task_id>/" in readme
    assert "分类、排名、商品、店铺名称、用户支付金额、成交件数、首次上榜" in readme
    assert "published_at IS NOT NULL" in design
    assert "dry-run 不写正式商品和店铺记录" in design
    assert "只包含成功完成的三级分类" in readme


def test_failure_docs_use_batch_and_category_identifiers() -> None:
    """Point troubleshooting at the current batch and category artifact layout."""

    # troubleshooting 必须能从日志身份直接定位 raw 和失败材料。
    troubleshooting = Path("docs/故障处理.md").read_text(encoding="utf-8")

    assert "<batch_id>" in troubleshooting
    assert "<category_run_id>" in troubleshooting
    assert "category-tree.json.gz" in troubleshooting
    assert "第 1～2 个" in troubleshooting
    assert "第 3 个" in troubleshooting
    assert "published_at IS NOT NULL" in troubleshooting


def test_makefile_exposes_a_compact_dynamic_delivery_surface() -> None:
    """Keep daily commands compact while retaining explicit mode parameters."""

    # makefile 是开发者日常使用的 npm-scripts 风格入口。
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "run:" in makefile
    assert "MODE ?= normal" in makefile
    assert "GUI ?= yes" in makefile
    assert "service:" in makefile
    assert "test-stage2:" not in makefile
    assert "run-cli:" not in makefile
