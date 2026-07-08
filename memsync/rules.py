"""規則層同步：human-authored CLAUDE.md ↔ AGENTS.md 互寫受管 block。

「人工規則」＝規則檔剝除所有 memsync 受管 block 後剩下的文字（防火牆：不把自己同步進去的規則再讀回來）。
互補方向：Claude 人工規則 → 寫進 Codex 的 AGENTS.md；Codex 人工規則 → 寫進 Claude 的 CLAUDE.md。
全域 Claude 側例外：人工規則來源優先讀獨立檔 ~/.claude/rules-source.md（session 不載入），
避免「人工來源段＋整合版受管區塊」在 CLAUDE.md 同檔雙份；該檔不存在時 fallback 舊行為（CLAUDE.md 殘文）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import blocks

HOME = Path.home()
CLAUDE_GLOBAL = HOME / ".claude" / "CLAUDE.md"
CODEX_GLOBAL = HOME / ".codex" / "AGENTS.md"
# Claude 側全域人工規則的獨立來源檔（Claude Code session 不載入此檔；規則只在這裡人工編輯）
CLAUDE_RULES_SOURCE = HOME / ".claude" / "rules-source.md"

_MEMSYNC_BLOCK = re.compile(r"\s*<!-- MEMSYNC:BEGIN .*?<!-- MEMSYNC:END [^>]*-->", re.DOTALL)


def human_rule(path: Path):
    """讀 rule 檔，剝掉所有 memsync 受管 block，回人工撰寫的規則文字（空則 None）。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = _MEMSYNC_BLOCK.sub("", text).strip()
    return stripped or None


def claude_global_rule_source():
    """Claude 側全域人工規則來源，回 (規則文字 or None, 實際採用的來源檔路徑)。

    優先讀獨立來源檔 ~/.claude/rules-source.md（人工規則唯一編輯面；仍過 human_rule
    剝除受管 block，防止整合版被誤貼回來源再吸回）。該檔不存在時 fallback 舊行為：
    CLAUDE.md 剝除受管區塊後的殘文——遷移前行為完全不變。路徑供呼叫端取 mtime（較新檔勝）。
    """
    if CLAUDE_RULES_SOURCE.exists():
        return human_rule(CLAUDE_RULES_SOURCE), CLAUDE_RULES_SOURCE
    return human_rule(CLAUDE_GLOBAL), CLAUDE_GLOBAL


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
