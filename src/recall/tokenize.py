from __future__ import annotations

import logging
import re

_jieba = None
_WORDISH = re.compile(r"\w", re.UNICODE)


def _ensure_jieba():
    global _jieba
    if _jieba is None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # jieba 源码在 3.12+ 有无害的 SyntaxWarning
            import jieba

            jieba.setLogLevel(logging.WARNING)
            jieba.initialize()
        _jieba = jieba
    return _jieba


def index_tokens(text: str, mode: str) -> str:
    """入库进 FTS5 的索引文本。

    trigram 模式直接用原文（3-gram 子串匹配）；
    jieba 模式存分词结果，两侧（入库/查询）用同一套分词保证 token 能对上。
    """
    if mode == "trigram":
        return text
    jb = _ensure_jieba()
    return " ".join(t for t in jb.cut_for_search(text) if _WORDISH.search(t))


def query_terms(query: str, mode: str) -> list[str]:
    """查询拆词，去重保序。"""
    q = query.strip()
    if not q:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    if mode == "trigram":
        candidates = q.split()
    else:
        candidates = _ensure_jieba().cut_for_search(q)
    for t in candidates:
        if _WORDISH.search(t) and t not in seen:
            seen.add(t)
            terms.append(t)
    return terms
