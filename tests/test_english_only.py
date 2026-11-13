"""Since 0.4.0 the whole project is English: no CJK characters may appear in any text file of the repository.

Tests that need non-ASCII data write it as \\u escapes (see the other test files)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "logs", "file_backups", "node_modules"}
TEXT_EXT = {".py", ".md", ".ps1", ".cmd", ".json", ".yml", ".yaml", ".txt", ".ini", ".toml", ".gitignore", ".gitattributes"}


def test_no_cjk_in_repository_text_files():
    offenders = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() not in TEXT_EXT and p.name not in {"LICENSE", ".gitignore", ".gitattributes"}:
            continue
        try:
            text = p.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if CJK.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{n}")
    assert not offenders, "CJK characters found (the project must stay English): " + ", ".join(offenders[:20])
