from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .tokenize import query_terms

DEFAULT_HL = ("«", "»")


@dataclass
class Hit:
    path: str
    kind: str | None
    line: int
    score: float | None
    snippet: str


def search(
    conn,
    query: str,
    limit: int = 20,
    tokenizer: str = "jieba",
    hl: tuple[str, str] = DEFAULT_HL,
) -> tuple[list[Hit], int, float, bool]:
    """返回 (hits, 总命中数, 耗时ms, 是否走了短词 LIKE 兜底)。"""
    t0 = time.perf_counter()
    terms = query_terms(query, tokenizer)
    if not terms:
        return [], 0, 0.0, False

    if tokenizer == "trigram" and any(len(t) < 3 for t in terms):
        hits = _search_like(conn, terms, limit, hl)
        return hits, len(hits), _ms(t0), True

    expr = " ".join('"%s"' % t.replace('"', '""') for t in terms)
    rows = conn.execute(
        "SELECT f.path, f.kind, c.start_line, c.text, bm25(chunks_fts) AS score "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "JOIN files f ON f.id = c.file_id "
        "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (expr, limit),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (expr,)
    ).fetchone()[0]
    hits = [
        Hit(r["path"], r["kind"], r["start_line"], round(-r["score"], 3), _snippet(r["text"], terms, hl))
        for r in rows
    ]
    return hits, total, _ms(t0), False


def _search_like(conn, terms, limit, hl) -> list[Hit]:
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    where = " AND ".join("c.text LIKE ? ESCAPE '\\'" for _ in terms)
    sql = (
        f"SELECT f.path, f.kind, c.start_line, c.text FROM chunks c "
        f"JOIN files f ON f.id = c.file_id WHERE {where} LIMIT ?"
    )
    rows = conn.execute(sql, [*("%" + esc(t) + "%" for t in terms), limit]).fetchall()
    return [Hit(r["path"], r["kind"], r["start_line"], None, _snippet(r["text"], terms, hl)) for r in rows]


def _snippet(text: str, terms: list[str], hl: tuple[str, str], width: int = 170) -> str:
    flat = " ".join(text.split())
    low = flat.casefold()
    pos = -1
    for t in terms:
        i = low.find(t.casefold())
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    start = max(0, pos - 40) if pos >= 0 else 0
    seg = flat[start : start + width]
    for t in sorted(terms, key=len, reverse=True):
        seg = re.sub(re.escape(t), lambda m: hl[0] + m.group(0) + hl[1], seg, flags=re.IGNORECASE)
    head = "…" if start > 0 else ""
    tail = "…" if start + width < len(flat) else ""
    return head + seg + tail


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
