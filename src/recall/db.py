from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("recall.db")

SCHEMA_VERSION = 1


def check_sqlite() -> None:
    v = sqlite3.sqlite_version_info
    if v < (3, 34, 0):
        raise SystemExit(
            f"SQLite 版本过低（{sqlite3.sqlite_version}），FTS5/trigram 分词器需要 >= 3.34"
        )


def _fts_mode_of(sql: str) -> str:
    return "trigram" if "trigram" in sql else "unicode61"


def _fts_ddl(mode: str) -> str:
    tok = "trigram" if mode == "trigram" else "unicode61"
    return (
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "tokens, content='chunks', content_rowid='id', "
        f"tokenize='{tok}')"
    )


def _retokenize(conn: sqlite3.Connection, mode: str) -> None:
    """切换分词模式后，用 chunks.text 重算所有 tokens。"""
    from .tokenize import index_tokens

    rows = conn.execute("SELECT id, text FROM chunks").fetchall()
    if not rows:
        return
    log.info("分词器切换，重算 %d 个块的 tokens…", len(rows))
    conn.executemany(
        "UPDATE chunks SET tokens=? WHERE id=?",
        [(index_tokens(t, mode), r["id"]) for r in rows for t in (r["text"],)],
    )


def _init_schema(conn: sqlite3.Connection, tokenizer: str, force_rebuild: bool) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            mtime_ns INTEGER,
            size INTEGER,
            hash TEXT,
            kind TEXT,
            n_chunks INTEGER DEFAULT 0,
            indexed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            start_line INTEGER NOT NULL,
            text TEXT NOT NULL,
            tokens TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
        """
    )
    row = conn.execute(
        "SELECT sql, (SELECT value FROM meta WHERE key='tokenizer') FROM sqlite_master WHERE name='chunks_fts'"
    ).fetchone()
    want = "trigram" if tokenizer == "trigram" else "unicode61"
    prev = row[1] if row else None

    # prev/want 都按 raw 分词名比较；_fts_mode_of 只用来核对 DDL 实际用的分词器
    need_rebuild = force_rebuild or (row is not None and (prev != tokenizer or _fts_mode_of(row[0]) != want))
    if row is None:
        conn.execute(_fts_ddl(tokenizer))
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    elif need_rebuild:
        if prev != tokenizer:
            _retokenize(conn, tokenizer)
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.execute(_fts_ddl(tokenizer))
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        log.info("全文索引已按 %s 分词器重建", tokenizer)

    conn.execute("INSERT OR REPLACE INTO meta VALUES('tokenizer', ?)", (tokenizer,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))


def connect(db_path: str, tokenizer: str = "jieba", force_rebuild_fts: bool = False) -> sqlite3.Connection:
    check_sqlite()
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn, tokenizer, force_rebuild_fts)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def delete_file_rows(conn: sqlite3.Connection, file_id: int) -> None:
    """删掉一个文件的所有块，并同步清掉 FTS 索引（delete 需要旧 tokens 值）。"""
    rows = conn.execute("SELECT id, tokens FROM chunks WHERE file_id=?", (file_id,)).fetchall()
    conn.executemany(
        "INSERT INTO chunks_fts(chunks_fts, rowid, tokens) VALUES('delete', ?, ?)",
        [(r["id"], r["tokens"]) for r in rows],
    )
    conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
