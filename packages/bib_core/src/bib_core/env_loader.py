from __future__ import annotations

import os
from pathlib import Path


def _parse_env_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[7:].strip()
    if "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if value and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def _load_env_file(path: Path, original_keys: set[str]) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if not parsed:
            continue
        key, value = parsed
        if key in original_keys:
            continue
        os.environ[key] = value
    return True


def load_repo_env(repo_root: Path | None = None) -> list[Path]:
    root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
    original_keys = set(os.environ)
    candidates = [
        root / "deploy" / ".env.runtime",
        root / "deploy" / ".env",
    ]
    loaded = []
    for path in candidates:
        if _load_env_file(path, original_keys):
            loaded.append(path)
    return loaded
