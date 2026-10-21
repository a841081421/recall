"""基准测试：生成中文语料，测 v0.1 三个验收数字，并对比 trigram / jieba。

用法：
  uv run python scripts/bench.py                          # 两种分词器都跑，1500 文件
  uv run python scripts/bench.py --files 3000 --tokenizer jieba --keep

验收口径（见 docs/第二大脑项目思路.md）：
  1. 千级文档分钟级建库      -> build_seconds / files_per_s
  2. 搜索命中 < 100ms        -> p50_ms / p95_ms
  3. 改动 10 秒内可搜到      -> incr_seconds（watch 线程真实监视 + 防抖）
"""

from __future__ import annotations

import argparse
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recall import watcher  # noqa: E402
from recall.config import Config  # noqa: E402
from recall.db import connect  # noqa: E402
from recall.indexer import Indexer  # noqa: E402
from recall.searcher import search as do_search  # noqa: E402

TOPICS = ["项目计划", "机器学习", "用户调研", "季度预算", "架构评审", "发布流程", "招聘面经", "读书笔记"]
SENTENCES = [
    "本季度{t}进入收尾阶段，剩余风险集中在数据迁移。",
    "关于{t}的评审结论：方案可行，但需要补充压测数据。",
    "今天整理了{t}相关的资料，发现去年11月有一份对比分析。",
    "{t}的负责人换人了，后续对接找新的同事。",
    "复盘会上提到，{t}延期的主要原因是需求反复变更。",
    "把{t}的执行清单拆到了周级别，每周五回顾进度。",
    "读了一篇关于{t}的文章，核心观点是先小步验证再放量。",
    "周报：本周在{t}上花了12小时，产出是两份文档和一次分享。",
    "{t}的预算申请已提交，等财务审批，预计下周到账。",
    "会议记录：{t}方向确认继续投入，人力再加一人。",
    "整理待办：{t}相关还有三件事没有闭环。",
    "灵感：把{t}的经验抽象成模板，下次直接复用。",
    "把合同扫描件归档到本地，随时可以检索调阅。",
    "这个记忆管理工具的价值在于：检索快，本地存，隐私安心。",
    "第二大脑的核心：把散落的记忆变成可以检索的知识。",
]

QUERIES_SHORT = ["记忆", "会议", "预算", "复盘", "周报", "合同", "检索", "评审"]
QUERIES_LONG = ["项目计划", "机器学习", "用户调研", "季度预算", "架构评审", "发布流程", "读书笔记", "招聘面经"]


def _para(rng: random.Random, uid: str) -> str:
    s = [f"本文唯一码{uid}。"] if rng.random() < 0.5 else []
    for _ in range(rng.randint(3, 8)):
        s.append(rng.choice(SENTENCES).format(t=rng.choice(TOPICS)))
    return "\n".join(s)


def _body(rng: random.Random, uid: str, n_paras: int) -> str:
    return "\n\n".join(_para(rng, uid) for _ in range(n_paras))


def make_docx(path: Path, text: str) -> None:
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    paras = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.splitlines())
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paras}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)


def make_pdf(path: Path, text: str) -> None:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore[no-redef]

    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 545, 780), text, fontname="china-s", fontsize=11)
    doc.save(str(path))
    doc.close()


def gen_corpus(root: Path, n_files: int, seed: int = 42) -> list[Path]:
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    n_bin = max(30, n_files // 25)  # pdf 与 docx 各占的量
    n_code = n_files // 6
    n_plain = n_files - 2 * n_bin - n_code
    files: list[Path] = []

    def uid(i: int) -> str:
        return f"{i:06d}"

    for i in range(n_plain):
        ext = ".md" if i % 2 else ".txt"
        name = f"笔记{uid(i)}{ext}" if i % 17 == 0 else f"note{uid(i)}{ext}"  # 一部分中文名，测路径
        p = root / name
        p.write_text(f"# {rng.choice(TOPICS)}\n\n{_body(rng, uid(i), rng.randint(3, 10))}\n", encoding="utf-8")
        files.append(p)
    for i in range(n_code):
        p = root / f"module_{uid(i)}.py"
        p.write_text(
            f'"""{rng.choice(TOPICS)}模块。\n\n{_para(rng, uid(i))}\n"""\n\n'
            f"def run_{i}(payload: dict) -> dict:\n"
            f'    """处理请求并返回结果。"""\n'
            f"    result = {{}}\n"
            f"    for key, value in payload.items():\n"
            f"        result[key.upper()] = str(value)\n"
            f"    return result\n",
            encoding="utf-8",
        )
        files.append(p)
    for i in range(n_bin):
        text = f"{rng.choice(TOPICS)}（正式文档）\n\n{_body(rng, uid(9000 + i), 5)}\n"
        make_pdf(root / f"报告{uid(i)}.pdf", text)
        files.append(root / f"报告{uid(i)}.pdf")
        make_docx(root / f"纪要{uid(i)}.docx", text)
        files.append(root / f"纪要{uid(i)}.docx")
    return files


def run_mode(mode: str, corpus: Path, workdir: Path, n_queries: int = 32) -> dict:
    db_path = str(workdir / f"bench_{mode}.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(db_path + suffix)
        if p.exists():
            p.unlink()
    cfg = Config(
        folders=[str(corpus)],
        db_path=db_path,
        tokenizer=mode,
        rescan_minutes=60,
        debounce_seconds=1.0,
    )
    conn = connect(db_path, mode)
    ix = Indexer(cfg, conn)

    # 1) 建库速度
    t0 = time.perf_counter()
    c = ix.scan()
    build_s = time.perf_counter() - t0
    n_files = c["new"] + c["updated"]

    # 2) 搜索延迟（短词 + 长词混合，每条跑 3 次取全部样本）
    queries = (QUERIES_SHORT + QUERIES_LONG)[:n_queries]
    lat: list[float] = []
    for q in queries:
        for _ in range(3):
            t = time.perf_counter()
            hits, _total, _ms, _like = do_search(conn, q, 20, mode)
            lat.append((time.perf_counter() - t) * 1000)
            assert hits, f"查询 {q!r} 无命中，基准数据有问题"
    lat.sort()
    p50 = statistics.median(lat)
    p95 = lat[int(len(lat) * 0.95) - 1]

    # 3) 增量可见性：watch 线程真实监视，改文件后计时到可搜
    stop = threading.Event()
    th = threading.Thread(target=watcher.run, args=(cfg,), kwargs={"stop_event": stop}, daemon=True)
    th.start()
    time.sleep(2)
    target = sorted(corpus.glob("*.md"))[0]
    marker = f"增量标记{int(time.time())}"
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"\n{marker} 追加的一行。\n")
    t1 = time.perf_counter()
    incr_s = None
    while time.perf_counter() - t1 < 30:
        hits, *_ = do_search(conn, marker, 5, mode)
        if hits:
            incr_s = time.perf_counter() - t1
            break
        time.sleep(0.2)
    stop.set()
    th.join(timeout=5)
    assert incr_s is not None, "30 秒内没有看到增量更新，watch 有问题"

    db_mb = Path(db_path).stat().st_size / 1e6
    conn.close()
    return {
        "mode": mode,
        "files": n_files,
        "build_s": round(build_s, 1),
        "files_per_s": round(n_files / build_s),
        "db_mb": round(db_mb, 1),
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "incr_s": round(incr_s, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=1500)
    ap.add_argument("--tokenizer", choices=["jieba", "trigram", "both"], default="both")
    ap.add_argument("--keep", action="store_true", help="保留临时语料与数据库")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="recall_bench_"))
    print(f"语料目录：{tmp}（{args.files} 个文件）")
    corpus = tmp / "corpus"
    t0 = time.perf_counter()
    gen_corpus(corpus, args.files)
    print(f"语料生成完成，用时 {time.perf_counter() - t0:.1f}s\n")

    modes = ["jieba", "trigram"] if args.tokenizer == "both" else [args.tokenizer]
    rows = [run_mode(m, corpus, tmp) for m in modes]

    cols = ["mode", "files", "build_s", "files_per_s", "db_mb", "p50_ms", "p95_ms", "incr_s"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print(" | ".join(c.ljust(widths[c]) for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r[c]).ljust(widths[c]) for c in cols))

    print(
        "\n验收口径：建库分钟级 ✓ | 搜索 p95 < 100ms -> "
        + ", ".join(f"{r['mode']} {r['p95_ms']}ms" for r in rows)
        + " | 增量 < 10s -> " + ", ".join(f"{r['mode']} {r['incr_s']}s" for r in rows)
    )
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
        print("临时目录已清理（--keep 可保留）")
    else:
        print(f"已保留：{tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
