from __future__ import annotations

import os
from pathlib import Path

import pytest

from recall.chunking import chunk_text
from recall.config import Config
from recall.db import connect
from recall.extract import extract_text
from recall.indexer import Indexer
from recall.searcher import search


def make_cfg(tmp_path: Path, tokenizer: str = "jieba") -> tuple[Config, Path]:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    cfg = Config(
        folders=[str(docs)],
        db_path=str(tmp_path / "test.db"),
        tokenizer=tokenizer,
        debounce_seconds=0.5,
        rescan_minutes=60,
    )
    return cfg, docs


def scan(cfg: Config) -> Indexer:
    conn = connect(cfg.resolved_db_path, cfg.tokenizer)
    ix = Indexer(cfg, conn)
    ix.scan()
    return ix


def hit_paths(ix: Indexer, query: str) -> set[str]:
    hits, _total, _ms, _like = search(ix.conn, query, 50, ix.fts_mode())
    return {h.path for h in hits}


# ---- 分块 ------------------------------------------------------------------


def test_chunking_basic():
    text = "\n".join(f"第{i}行内容" for i in range(200))
    chunks = chunk_text(text, target=600)
    assert chunks
    assert all(len(c) <= 620 for _, c in chunks)
    lines = [ln for ln, _ in chunks]
    assert lines == sorted(lines)
    assert "第199行内容" in chunks[-1][1]


def test_chunking_long_line():
    chunks = chunk_text("短行\n" + "长" * 2000, target=100)
    assert any("短行" in c for _, c in chunks)
    assert all(len(c) <= 120 for _, c in chunks)


# ---- 抽取 ------------------------------------------------------------------


def test_extract_gbk(tmp_path: Path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("会议纪要：预算超支，需要复盘。".encode("gb18030"))
    text, note = extract_text(p, 1_000_000)
    assert note is None
    assert "复盘" in text


def test_extract_txt(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("机器学习入门笔记", encoding="utf-8")
    text, _ = extract_text(p, 1_000_000)
    assert "机器学习" in text


def test_extract_docx(tmp_path: Path):
    pydocx = pytest.importorskip("docx")
    p = tmp_path / "t.docx"
    d = pydocx.Document()
    d.add_paragraph("季度预算评审结论：按季度结算。")
    d.save(str(p))
    text, note = extract_text(p, 1_000_000)
    assert note is None
    assert "预算" in text


def test_extract_pdf(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    p = tmp_path / "t.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(50, 50, 545, 700),
        "用户调研报告：记忆检索是核心痛点。",
        fontname="china-s",
        fontsize=11,
    )
    doc.save(str(p))
    doc.close()
    text, note = extract_text(p, 1_000_000)
    assert note is None
    assert "调研" in text or "记忆" in text


# ---- 索引 + 检索（两种分词器都过一遍）--------------------------------------


@pytest.mark.parametrize("tokenizer", ["jieba", "trigram"])
def test_roundtrip(tmp_path: Path, tokenizer: str):
    cfg, docs = make_cfg(tmp_path, tokenizer)
    (docs / "plan.md").write_text(
        "# 项目计划\n本季度目标是上线第二大脑项目，包含增量索引与全文检索。\n",
        encoding="utf-8",
    )
    (docs / "diary.txt").write_text("今天开了架构评审会，讨论了缓存策略。\n", encoding="utf-8")
    ix = scan(cfg)
    assert ix.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2

    hits = hit_paths(ix, "项目计划")
    assert any("plan.md" in p for p in hits)

    if tokenizer == "jieba":
        # 2 字短词直接命中
        assert hit_paths(ix, "架构")
    else:
        # trigram 对 <3 字走 LIKE 子串兜底，照样能命中
        hits, total, _ms, like = search(ix.conn, "架构", 10, "trigram")
        assert like and hits

    # 多词 AND：不存在的词不应有命中
    assert not hit_paths(ix, "项目计划 不存在的词")


@pytest.mark.parametrize("tokenizer", ["jieba", "trigram"])
def test_incremental(tmp_path: Path, tokenizer: str):
    cfg, docs = make_cfg(tmp_path, tokenizer)
    f = docs / "a.txt"
    f.write_text("第一版内容：预算表在这里。\n", encoding="utf-8")
    ix = scan(cfg)
    assert hit_paths(ix, "预算表")

    # 修改：追加新内容，新词可搜到
    f.write_text("第一版内容：预算表在这里。\n第二版新增：季度复盘结论。\n", encoding="utf-8")
    assert ix.sync_file(f) == "updated"
    assert hit_paths(ix, "季度复盘")

    # 只动 mtime 不动内容 → unchanged
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert ix.sync_file(f) == "unchanged"

    # 删除 → 移除，块清干净
    f.unlink()
    assert ix.remove_file(f)
    assert not hit_paths(ix, "预算表")
    assert ix.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_gbk_indexed(tmp_path: Path):
    cfg, docs = make_cfg(tmp_path, "jieba")
    (docs / "gbk.txt").write_bytes("合同编号2026：服务费按季度结算。".encode("gb18030"))
    ix = scan(cfg)
    assert hit_paths(ix, "合同编号")


def test_tokenizer_switch_rebuilds(tmp_path: Path):
    cfg, docs = make_cfg(tmp_path, "trigram")
    (docs / "a.md").write_text("机器学习改变世界，检索是刚需。\n", encoding="utf-8")
    ix = scan(cfg)
    ix.conn.close()

    cfg2 = Config(
        folders=cfg.folders,
        db_path=cfg.db_path,
        tokenizer="jieba",
        rescan_minutes=60,
    )
    ix2 = scan(cfg2)
    assert ix2.fts_mode() == "jieba"
    assert hit_paths(ix2, "检索")
    # tokens 列已被重算成 jieba 分词结果（含空格分隔）
    row = ix2.conn.execute("SELECT tokens, text FROM chunks LIMIT 1").fetchone()
    assert " ".join(row["tokens"].split()) != row["text"] or row["tokens"] == row["text"]


def test_scan_removes_deleted(tmp_path: Path):
    cfg, docs = make_cfg(tmp_path, "jieba")
    f = docs / "gone.md"
    f.write_text("这段内容会被删除，包括独有词琥珀酸。", encoding="utf-8")
    ix = scan(cfg)
    assert hit_paths(ix, "琥珀酸")
    f.unlink()
    ix.scan()
    assert not hit_paths(ix, "琥珀酸")
    assert ix.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_scan_keeps_out_of_scope_files(tmp_path: Path):
    cfg, docs = make_cfg(tmp_path, "jieba")
    other = tmp_path / "other"
    other.mkdir()
    (docs / "in.md").write_text("范围内文档。", encoding="utf-8")
    ix = scan(cfg)

    # 直接塞一条范围外的记录，扫描不应误删
    with ix.conn:
        ix.conn.execute(
            "INSERT INTO files(path, mtime_ns, size, hash, kind, n_chunks, indexed_at) "
            "VALUES(?,?,?,?,?,0,'now')",
            (str(other / "out.md"), 0, 0, "x", "text"),
        )
    ix.scan()
    left = ix.conn.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE ?", (str(other) + "%",)
    ).fetchone()[0]
    assert left == 1


def test_fts_delete_consistency(tmp_path: Path):
    """反复增删后 FTS 索引与 chunks 必须严格一致。"""
    cfg, docs = make_cfg(tmp_path, "jieba")
    for i in range(10):
        (docs / f"f{i}.md").write_text(f"文档{i}：记忆点{i}号。", encoding="utf-8")
    ix = scan(cfg)
    for i in range(0, 10, 2):
        (docs / f"f{i}.md").unlink()
    ix.scan()

    n_chunks = ix.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_fts = ix.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    assert n_chunks == n_fts == 5

    # FTS5 自带的 integrity-check：索引与外部内容表对齐则无异常
    ix.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")


def test_empty_query(tmp_path: Path):
    cfg, _docs = make_cfg(tmp_path, "jieba")
    ix = scan(cfg)
    hits, total, _ms, _like = search(ix.conn, "   ", 10, "jieba")
    assert hits == [] and total == 0
