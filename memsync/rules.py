"""規則層同步：human-authored CLAUDE.md ↔ AGENTS.md 互寫受管 block。

「人工規則」＝規則檔剝除所有 memsync 受管 block 後剩下的文字（防火牆：不把自己同步進去的規則再讀回來）。
互補方向：Claude 人工規則 → 寫進 Codex 的 AGENTS.md；Codex 人工規則 → 寫進 Claude 的 CLAUDE.md。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import blocks

HOME = Path.home()
CLAUDE_GLOBAL = HOME / ".claude" / "CLAUDE.md"
CODEX_GLOBAL = HOME / ".codex" / "AGENTS.md"

_MEMSYNC_BLOCK = re.compile(r"\s*<!-- MEMSYNC:BEGIN .*?<!-- MEMSYNC:END [^>]*-->", re.DOTALL)


def human_rule(path: Path):
    """讀 rule 檔，剝掉所有 memsync 受管 block，回人工撰寫的規則文字（空則 None）。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = _MEMSYNC_BLOCK.sub("", text).strip()
    return stripped or None


def rule_op(path: Path, body: str, pid: str, source_label: str, ts: str):
    kind = "rules"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new_h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    ok, reason = blocks.integrity_check(old, kind, pid)
    base = {"target": str(path), "typ": "rules", "kind": kind, "project": pid, "hash": new_h, "reason": "", "warn": ""}
    if not ok:
        return {**base, "action": "BLOCKED", "reason": reason, "old": old, "new": old}
    if blocks.existing_hash(old, kind, pid) == new_h:
        return {**base, "action": "unchanged", "old": old, "new": old}
    nb = blocks.render_rule_block(body, pid, source_label, ts)
    new = blocks.upsert(old, nb, kind, pid)
    return {**base, "action": ("create" if not path.exists() else "update"),
            "old": old, "new": new, "warn": blocks.growth_warn(old, new, kind, pid)}
