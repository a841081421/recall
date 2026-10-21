from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from . import watcher
from .config import (
    APP_DIR,
    add_folder,
    Config,
    find_config_path,
    load_config,
    remove_folder,
    save_config,
)
from .db import connect
from .indexer import Indexer
from .searcher import DEFAULT_HL, search as do_search
from .util import enable_utf8_stdout, fmt_bytes, setup_logging

log = logging.getLogger("recall")

LAST_SEARCH = APP_DIR / "last_search.json"


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="recall",
        description="Everything 找文件名，Recall 找记忆。本机、即时、可问答的第二大脑。",
    )
    parser.add_argument("--config", help="配置文件路径（默认 ./recall.yaml 或 ~/.recall/config.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="建库/追赶扫描（未变化的文件自动跳过）")
    p.add_argument("--reset", action="store_true", help="清空数据库后全量重建")
    p.add_argument("--rebuild-fts", action="store_true", help="仅重建全文索引（不重抽文件）")

    p = sub.add_parser("search", help="全文检索")
    p.add_argument("query", nargs="+", help="搜索词，空格分隔多个词（AND）")
    p.add_argument("-n", "--limit", type=int, default=20)

    sub.add_parser("stats", help="库概况")
    sub.add_parser("watch", help="常驻监视（启动时先追赶扫描）")

    p = sub.add_parser("config", help="管理配置（文件夹列表）")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("value", nargs="?", help="add/remove 时的文件夹路径")

    p = sub.add_parser("open", help="打开上次搜索结果的第 N 条")
    p.add_argument("n", type=int)

    args = parser.parse_args(argv)
    setup_logging(args.verbose, logfile=APP_DIR / "recall.log" if args.cmd == "watch" else None)

    try:
        if args.cmd == "index":
            return _cmd_index(args)
        if args.cmd == "search":
            return _cmd_search(args)
        if args.cmd == "stats":
            return _cmd_stats(args)
        if args.cmd == "watch":
            return _cmd_watch(args)
        if args.cmd == "config":
            return _cmd_config(args)
        if args.cmd == "open":
            return _cmd_open(args)
    except KeyboardInterrupt:
        print("\n已退出。")
        return 130
    except (BrokenPipeError, OSError) as e:
        log.error("%s", e)
        return 1
    return 0


def _load(args) -> tuple[Config, Path]:
    cfg, path = load_config(args.config)
    return cfg, path


def _cmd_index(args) -> int:
    cfg, _ = _load(args)
    if not cfg.folders and not args.rebuild_fts:
        print("还没有配置要索引的文件夹。先运行：recall config add <文件夹路径>")
        return 1
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(cfg.resolved_db_path + suffix)
            if p.exists():
                p.unlink()
        print(f"已清空数据库：{cfg.resolved_db_path}")
    conn = connect(cfg.resolved_db_path, cfg.tokenizer, force_rebuild_fts=args.rebuild_fts)
    try:
        if args.rebuild_fts:
            print("全文索引已重建。")
            return 0
        ix = Indexer(cfg, conn)
        c = ix.scan()
        print(
            f"完成：候选 {c['candidates']} ｜ 新增 {c['new']} ｜ 更新 {c['updated']} ｜ "
            f"未变化 {c['unchanged']} ｜ 跳过 {c['skipped']} ｜ 移除 {c['removed']} ｜ "
            f"用时 {c['seconds']}s"
        )
        return 0
    finally:
        conn.close()


def _cmd_search(args) -> int:
    cfg, _ = _load(args)
    conn = connect(cfg.resolved_db_path, cfg.tokenizer)
    try:
        q = " ".join(args.query)
        n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if n_files == 0:
            print("库还是空的，先跑：recall index")
            return 1
        use_ansi = sys.stdout.isatty()
        hl = ("\x1b[7m", "\x1b[0m") if use_ansi else DEFAULT_HL
        hits, total, ms, like_fallback = do_search(conn, q, args.limit, cfg.tokenizer, hl=hl)
        LAST_SEARCH.parent.mkdir(parents=True, exist_ok=True)
        LAST_SEARCH.write_text(
            json.dumps({"query": q, "paths": [h.path for h in hits]}, ensure_ascii=False),
            encoding="utf-8",
        )
        note = "（含短词，走子串匹配，无相关性排序）" if like_fallback else ""
        print(f"共 {total:,} 条命中，用时 {ms} ms{note}")
        if not hits:
            print("（无命中）换个词，或确认目标文件夹已 config add + index。")
            return 0
        for i, h in enumerate(hits, 1):
            print(f"{i:>3}. {h.path}  [{h.kind}]  L{h.line}")
            print(f"     {h.snippet}")
        if len(hits) < total:
            print(f"…还有 {total - len(hits):,} 条，用 -n 调大。打开某条：recall open <序号>")
        return 0
    finally:
        conn.close()


def _cmd_stats(args) -> int:
    cfg, path = _load(args)
    conn = connect(cfg.resolved_db_path, cfg.tokenizer)
    try:
        s = Indexer(cfg, conn).stats()
    finally:
        conn.close()
    kinds = " / ".join(f"{k or '未知'} {v}" for k, v in sorted(s["by_kind"].items())) or "—"
    print(f"库：{cfg.resolved_db_path}（{fmt_bytes(s['db_size'])}，分词：{s['tokenizer']}）")
    print(f"配置：{path}")
    print(f"文件：{s['files']:,}（{kinds}）｜ 空内容 {s['zero_chunk_files']}")
    print(f"块数：{s['chunks']:,} ｜ 文本量：{fmt_bytes(s['text_chars'])}")
    print(f"最近索引：{s['last_indexed'] or '—'}")
    if s["per_root"]:
        print("目录：")
        for root, n in s["per_root"]:
            print(f"  {root:<50} {n:>6,} 文件")
    return 0


def _cmd_watch(args) -> int:
    cfg, _ = _load(args)
    if not cfg.folders:
        print("还没有配置要监视的文件夹。先运行：recall config add <文件夹路径>")
        return 1
    watcher.run(cfg)
    return 0


def _cmd_config(args) -> int:
    if args.action == "add":
        if not args.value:
            print("用法：recall config add <文件夹路径>")
            return 1
        cfg, path = add_folder(args.value, args.config)
        print(f"已添加：{args.value}")
        print(f"配置文件：{path}（共 {len(cfg.folders)} 个目录）")
        print("下一步：recall index")
        return 0
    if args.action == "remove":
        if not args.value:
            print("用法：recall config remove <文件夹路径>")
            return 1
        cfg, path = remove_folder(args.value, args.config)
        print(f"已移除（若存在）。当前 {len(cfg.folders)} 个目录，配置文件：{path}")
        print("提示：被移除目录的旧数据会在下次 recall index 时按当前目录范围清理。")
        return 0
    # list
    cfg, path = _load(args)
    print(f"配置文件：{path}{'（尚不存在）' if not path.exists() else ''}")
    print(f"数据库：{cfg.resolved_db_path}")
    print(f"分词器：{cfg.tokenizer}")
    print(f"目录（{len(cfg.folders)}）：")
    for f in cfg.folders:
        print(f"  {f}")
    if not cfg.folders:
        print("  （空）用 recall config add <路径> 添加")
    return 0


def _cmd_open(args) -> int:
    if not LAST_SEARCH.exists():
        print("还没有搜索记录，先 recall search 一下。")
        return 1
    data = json.loads(LAST_SEARCH.read_text(encoding="utf-8"))
    paths = data.get("paths", [])
    if not (1 <= args.n <= len(paths)):
        print(f"序号超出范围（当前结果 1-{len(paths)}）。")
        return 1
    p = paths[args.n - 1]
    _open_in_shell(p)
    print(f"已打开：{p}")
    return 0


def _open_in_shell(p: str) -> None:
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["open", p], check=False)
    else:
        subprocess.run(["xdg-open", p], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
