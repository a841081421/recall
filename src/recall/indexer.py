from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .chunking import chunk_text
from .config import KIND_BY_EXT, Config
from .db import delete_file_rows, tx
from .extract import extract_text
from .tokenize import index_tokens
from .util import norm_path, sha256_file

log = logging.getLogger("recall.index")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Indexer:
    def __init__(self, cfg: Config, conn):
        self.cfg = cfg
        self.conn = conn
        self.max_bytes = int(cfg.max_file_size_mb * 1024 * 1024)

    # ---- 候选判断 -----------------------------------------------------------

    def kind_of(self, path) -> str | None:
        return KIND_BY_EXT.get(Path(path).suffix.lower())

    def is_candidate(self, path) -> bool:
        p = Path(path)
        if p.name.startswith((".", "~$")):
            return False
        if self.kind_of(p) is None:
            return False
        return not self.cfg.is_excluded_file(str(p))

    # ---- 单文件 -------------------------------------------------------------

    def sync_file(self, path) -> str:
        """把单个文件对齐到库里。返回 new / updated / unchanged / skipped / ignored。"""
        p = Path(path)
        if not self.is_candidate(p):
            return "ignored"
        try:
            st = p.stat()
        except OSError:
            return "skipped"
        if st.st_size > self.max_bytes:
            log.debug("超限跳过（>%sMB）：%s", self.cfg.max_file_size_mb, p)
            return "skipped"

        np = norm_path(p)
        row = self.conn.execute(
            "SELECT id, mtime_ns, size, hash FROM files WHERE path=?", (np,)
        ).fetchone()
        if row and row["mtime_ns"] == st.st_mtime_ns and row["size"] == st.st_size:
            return "unchanged"

        try:
            h = sha256_file(p)
        except OSError as e:
            log.warning("读取失败 %s：%s", p, e)
            return "skipped"
        if row and row["hash"] == h:
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE files SET mtime_ns=?, size=? WHERE id=?",
                    (st.st_mtime_ns, st.st_size, row["id"]),
                )
            return "unchanged"

        text, note = extract_text(p, self.cfg.max_text_chars)
        if note:
            log.info("备注 %s：%s", np, note)
        chunks = chunk_text(text, self.cfg.chunk_chars)
        mode = self.fts_mode()
        rows = [
            (o, line, body, index_tokens(body, mode))
            for o, (line, body) in enumerate(chunks)
        ]
        kind = self.kind_of(p)
        with tx(self.conn):
            if row:
                delete_file_rows(self.conn, row["id"])
            self.conn.execute(
                "INSERT INTO files(path, mtime_ns, size, hash, kind, n_chunks, indexed_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns, "
                "size=excluded.size, hash=excluded.hash, kind=excluded.kind, "
                "n_chunks=excluded.n_chunks, indexed_at=excluded.indexed_at",
                (np, st.st_mtime_ns, st.st_size, h, kind, len(rows), _now()),
            )
            fid = self.conn.execute("SELECT id FROM files WHERE path=?", (np,)).fetchone()[0]
            self.conn.executemany(
                "INSERT INTO chunks(file_id, ordinal, start_line, text, tokens) VALUES(?,?,?,?,?)",
                [(fid, o, l, t, k) for o, l, t, k in rows],
            )
            self.conn.execute(
                "INSERT INTO chunks_fts(rowid, tokens) SELECT id, tokens FROM chunks WHERE file_id=?",
                (fid,),
            )
        return "updated" if row else "new"

    def remove_file(self, path) -> bool:
        np = norm_path(path)
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (np,)).fetchone()
        if not row:
            return False
        with tx(self.conn):
            delete_file_rows(self.conn, row["id"])
            self.conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
        return True

    # ---- 全量追赶扫描 --------------------------------------------------------

    def scan(self) -> dict:
        """遍历配置目录，与库对齐：新增/更新/清理已删除。跳过未变化文件。"""
        t0 = time.perf_counter()
        counters = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "ignored": 0, "removed": 0}
        roots = list(self.cfg.folders)

        candidates: list[str] = []
        for root in roots:
            if not os.path.isdir(root):
                log.warning("配置的文件夹不存在：%s", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not self.cfg.is_excluded_dir(d)]
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    if self.is_candidate(p):
                        candidates.append(p)

        total = len(candidates)
        for i, p in enumerate(candidates, 1):
            counters[self.sync_file(p)] += 1
            if i % 200 == 0 or i == total:
                rate = i / max(time.perf_counter() - t0, 1e-6)
                log.info("进度 %d/%d（%.0f 文件/秒）", i, total, rate)

        # 清理库里有、盘上没了的文件（仅限当前配置目录之下，别处的不动）
        seen = {os.path.normcase(c) for c in candidates}
        root_norms = [os.path.normcase(os.path.normpath(r)) + os.sep for r in roots if os.path.isdir(r)]
        for r in self.conn.execute("SELECT id, path FROM files").fetchall():
            rp = r["path"]
            ncp = os.path.normcase(rp)
            if ncp in seen:
                continue
            if any(ncp.startswith(rn) for rn in root_norms):
                if self.remove_file(rp):
                    counters["removed"] += 1

        counters["seconds"] = round(time.perf_counter() - t0, 2)
        counters["candidates"] = total
        return counters

    # ---- 查询 / 统计 ---------------------------------------------------------

    def fts_mode(self) -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key='tokenizer'").fetchone()
        return row[0] if row else self.cfg.tokenizer

    def stats(self) -> dict:
        c = self.conn
        files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        by_kind = {r[0]: r[1] for r in c.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind")}
        n_chunks, text_chars = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(text)),0) FROM chunks"
        ).fetchone()
        zero = c.execute("SELECT COUNT(*) FROM files WHERE n_chunks=0").fetchone()[0]
        last = c.execute("SELECT MAX(indexed_at) FROM files").fetchone()[0]
        db_size = 0
        dbp = self.cfg.resolved_db_path
        if os.path.exists(dbp):
            db_size = os.path.getsize(dbp)
            for suffix in ("-wal", "-shm"):
                pp = dbp + suffix
                if os.path.exists(pp):
                    db_size += os.path.getsize(pp)
        per_root = []
        for root in self.cfg.folders:
            rn = os.path.normcase(os.path.normpath(root))
            n = sum(1 for (p,) in c.execute("SELECT path FROM files") if os.path.normcase(p).startswith(rn))
            per_root.append((root, n))
        return {
            "files": files,
            "by_kind": by_kind,
            "chunks": n_chunks,
            "text_chars": text_chars,
            "zero_chunk_files": zero,
            "last_indexed": last,
            "db_size": db_size,
            "tokenizer": self.fts_mode(),
            "per_root": per_root,
        }
