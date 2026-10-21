from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILENAME = "recall.yaml"
APP_DIR = Path(os.environ.get("RECALL_HOME", str(Path.home() / ".recall")))

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".srt", ".lrc"}
CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
    ".bat", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".xml", ".html", ".htm", ".css", ".scss", ".vue", ".lua", ".r",
    ".pl", ".properties", ".env", ".gradle", ".cmake",
}
KIND_BY_EXT: dict[str, str] = {}
for _e in TEXT_EXTS:
    KIND_BY_EXT[_e] = "text"
for _e in CODE_EXTS:
    KIND_BY_EXT[_e] = "code"
for _e in (".pdf",):
    KIND_BY_EXT[_e] = "pdf"
for _e in (".docx",):
    KIND_BY_EXT[_e] = "docx"

EXCLUDED_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "$RECYCLE.BIN", "System Volume Information",
}

TEMPLATE = """\
# Recall 配置（由 recall config add 生成）
# 路径写法：推荐正斜杠 D:/Notes；若用反斜杠请加单引号 'D:\\Notes'
folders: []
db_path: ""
tokenizer: jieba
max_file_size_mb: 20
max_text_chars: 2000000
chunk_chars: 600
exclude_globs:
  - "**/*.tmp"
  - "**/*~"
rescan_minutes: 30
debounce_seconds: 2
"""


@dataclass
class Config:
    folders: list[str] = field(default_factory=list)
    db_path: str = ""
    tokenizer: str = "jieba"
    exclude_globs: list[str] = field(default_factory=list)
    max_file_size_mb: float = 20.0
    max_text_chars: int = 2_000_000
    chunk_chars: int = 600
    rescan_minutes: int = 30
    debounce_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.tokenizer not in ("jieba", "trigram"):
            raise ValueError(f"tokenizer 只能是 jieba 或 trigram，当前是 {self.tokenizer!r}")

    @property
    def resolved_db_path(self) -> str:
        return self.db_path or str(APP_DIR / "recall.db")

    def is_excluded_dir(self, name: str) -> bool:
        return name.startswith(".") or name in EXCLUDED_DIRS

    def is_excluded_file(self, path: str) -> bool:
        name = os.path.basename(path)
        if name.startswith((".", "~$")):
            return True
        rel = Path(path).as_posix()
        for g in self.exclude_globs:
            if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, os.path.basename(g)):
                return True
        parts = Path(path).parts
        return any(p in EXCLUDED_DIRS for p in parts[:-1])


def find_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    cwd_cfg = Path.cwd() / CONFIG_FILENAME
    if cwd_cfg.exists():
        return cwd_cfg
    return APP_DIR / "config.yaml"


def load_config(explicit: str | None = None) -> tuple[Config, Path]:
    path = find_config_path(explicit)
    cfg = Config()
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = Config.__dataclass_fields__
        cfg = Config(**{k: v for k, v in data.items() if k in known})
    cfg.folders = [os.path.abspath(os.path.expanduser(str(f))) for f in cfg.folders]
    return cfg, path


def save_config(cfg: Config, path: Path) -> None:
    data = {
        "folders": cfg.folders,
        "db_path": cfg.db_path,
        "tokenizer": cfg.tokenizer,
        "max_file_size_mb": cfg.max_file_size_mb,
        "max_text_chars": cfg.max_text_chars,
        "chunk_chars": cfg.chunk_chars,
        "exclude_globs": cfg.exclude_globs,
        "rescan_minutes": cfg.rescan_minutes,
        "debounce_seconds": cfg.debounce_seconds,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "" if path.exists() else "# Recall 配置（由 recall config add 生成）\n"
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def add_folder(folder: str, explicit: str | None = None) -> tuple[Config, Path]:
    cfg, path = load_config(explicit)
    norm = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(norm):
        raise NotADirectoryError(f"文件夹不存在：{norm}")
    if norm not in cfg.folders:
        cfg.folders.append(norm)
    save_config(cfg, path)
    return cfg, path


def remove_folder(folder: str, explicit: str | None = None) -> tuple[Config, Path]:
    cfg, path = load_config(explicit)
    norm = os.path.normcase(os.path.abspath(os.path.expanduser(folder)))
    cfg.folders = [f for f in cfg.folders if os.path.normcase(f) != norm]
    save_config(cfg, path)
    return cfg, path
