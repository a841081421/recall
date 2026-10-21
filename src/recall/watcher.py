from __future__ import annotations

import logging
import queue
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .db import connect
from .indexer import Indexer

log = logging.getLogger("recall.watch")


def _fmt_counters(c: dict) -> str:
    return (
        f"新增 {c['new']} 更新 {c['updated']} 未变 {c['unchanged']} "
        f"跳过 {c['skipped']} 移除 {c['removed']}（{c.get('seconds', 0)}s）"
    )


class _Handler(FileSystemEventHandler):
    def __init__(self, ix: Indexer):
        super().__init__()
        self.ix = ix
        self.q: "queue.Queue[tuple[str, str]]" = queue.Queue()

    def on_created(self, e):
        self._enqueue(e, "changed")

    def on_modified(self, e):
        self._enqueue(e, "changed")

    def on_deleted(self, e):
        self._enqueue(e, "deleted")

    def on_moved(self, e):
        if e.is_directory:
            return
        self._enqueue(e, "deleted")  # 先按源路径清掉
        self.q.put((str(e.dest_path), "changed"))

    def _enqueue(self, e, kind: str) -> None:
        if e.is_directory:
            return
        p = str(e.src_path)
        if not self.ix.is_candidate(p):
            return
        self.q.put((p, kind))


def run(cfg, stop_event: threading.Event | None = None) -> None:
    """常驻监视：启动先追赶扫描，之后事件防抖入库 + 周期重扫兜底。"""
    conn = connect(cfg.resolved_db_path, cfg.tokenizer)
    ix = Indexer(cfg, conn)
    log.info("[watch] 启动追赶扫描…")
    log.info("[watch] 追赶完成：%s", _fmt_counters(ix.scan()))

    handler = _Handler(ix)
    obs = Observer()
    for f in cfg.folders:
        obs.schedule(handler, f, recursive=True)
    obs.start()
    log.info("[watch] 监视中：%s（Ctrl+C 退出）", "、".join(cfg.folders))

    stop = stop_event or threading.Event()
    pending: dict[str, tuple[str, float]] = {}
    next_rescan = time.monotonic() + cfg.rescan_minutes * 60
    try:
        while not stop.is_set():
            try:
                p, kind = handler.q.get(timeout=0.25)
            except queue.Empty:
                p = None
            if p:
                pending[p] = (kind, time.monotonic())

            now = time.monotonic()
            due = [k for k, (_, ts) in pending.items() if now - ts >= cfg.debounce_seconds]
            for path in due:
                kind2, _ = pending.pop(path)
                if kind2 == "deleted":
                    if ix.remove_file(path):
                        log.info("[watch] 移除 %s", path)
                else:
                    act = ix.sync_file(path)
                    if act in ("new", "updated"):
                        log.info("[watch] %s %s", {"new": "新增", "updated": "更新"}[act], path)

            if now >= next_rescan:
                log.info("[watch] 周期重扫：%s", _fmt_counters(ix.scan()))
                next_rescan = time.monotonic() + cfg.rescan_minutes * 60
    finally:
        obs.stop()
        obs.join(timeout=2)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
        log.info("[watch] 已退出")
