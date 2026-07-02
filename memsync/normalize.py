"""Normalizer：RawItem → CanonicalEntry。讀記憶內容、正規化、算 content_sha256。

content_sha256 用正規化純文字算（去 frontmatter、統一 CRLF→LF、去行尾空白、去頭尾空行），
以免同一事實因空白差異 hash 不同 → 是 generation 疊代與去重的基準。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_SKIP_BASENAMES = {"MEMORY.md", "memory.md"}  # 索引檔本身不同步（它只是其他顆的指標）
_SLUG_RE = re.compile(r"[^A-Za-z0-9._一-鿿-]+")


def slug(s: str) -> str:
    s = _SLUG_RE.sub("-", (s or "").strip()).strip("-._")
    return s[:80] or "item"


@dataclass
class Entry:
    logical_id: str
    origin_side: str       # 'claude' | 'codex'
    origin_path: str
    name: str
    etype: str
    description: str
    body: str
    content_sha256: str
    source_mtime: float = 0.0   # 來源檔最後修改時間；整合衝突時「兩來源檔誰較新」的判斷依據


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _norm_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return s.strip()


def _parse_frontmatter(text: str):
    mm = _FM_RE.match(text)
    meta, body = {}, text
    if mm:
        body = text[mm.end():]
        for line in mm.group(1).split("\n"):
            kv = _KV_RE.match(line)
            if kv:
                meta.setdefault(kv.group(1), kv.group(2).strip())
    return meta, body


def entry_from_claude_memory(rawitem):
    p = Path(rawitem.origin_path)
    # 防火牆 #1：絕不採集 memsync 自己寫出去的東西（否則回授無限增生）
    if p.name in _SKIP_BASENAMES or p.name.startswith("memsync-"):
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    meta, body = _parse_frontmatter(text)
    if meta.get("origin") == "memsync":
        return None
    body_n = _norm_text(body)
    if not body_n:
        return None
    sha = hashlib.sha256(body_n.encode("utf-8")).hexdigest()
    return Entry(
        logical_id=rawitem.logical_id,
        origin_side="claude",
        origin_path=str(p),
        name=meta.get("name") or p.stem,
        etype=meta.get("type") or "memory",
        description=meta.get("description") or "",
        body=body_n,
        content_sha256=sha,
        source_mtime=_mtime(p),
    )


def entry_from_codex_rollout(rawitem):
    """Codex rollout_summaries/*.md -> Entry。跳過開頭 kv header，取 # 標題當 name、正文當 body。"""
    p = Path(rawitem.origin_path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = text.split("\n")
    i = 0
    while i < len(lines) and re.match(r"^[a-z_]+:\s", lines[i]):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    rest = "\n".join(lines[i:])
    body_n = _norm_text(rest)
    if not body_n:
        return None
    tm = re.search(r"^#\s+(.+)$", rest, re.MULTILINE)
    name = tm.group(1).strip() if tm else p.stem
    dm = re.search(r"Rollout context[:：]\s*(.+)", rest)
    description = dm.group(1).strip()[:200] if dm else ""
    sha = hashlib.sha256(body_n.encode("utf-8")).hexdigest()
    return Entry(
        logical_id=rawitem.logical_id,
        origin_side="codex",
        origin_path=str(p),
        name=name,
        etype="memory",
        description=description,
        body=body_n,
        content_sha256=sha,
        source_mtime=_mtime(p),
    )
