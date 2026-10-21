from __future__ import annotations


def chunk_text(text: str, target: int = 600) -> list[tuple[int, str]]:
    """按行聚成约 target 字符的块，返回 (起始行号1-based, 块文本)。

    超长单行在 target 处硬切；空行丢弃；全空白内容返回空列表。
    """
    if target <= 0:
        raise ValueError("target 必须为正")
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_len = 0
    start_line = 1

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                chunks.append((start_line, joined))
            buf = []
            buf_len = 0

    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line:
            continue
        if len(line) > target:
            flush()
            for off in range(0, len(line), target):
                pieces = line[off : off + target].strip()
                if pieces:
                    chunks.append((i, pieces))
            continue
        if buf and buf_len + len(line) > target:
            flush()
        if not buf:
            start_line = i
        buf.append(line)
        buf_len += len(line) + 1
    flush()
    return chunks
