"""Collectors（唯讀採集器）。

reader_claude：~/.claude/projects/*/memory/*.md（每專案記憶）+ ~/.claude/CLAUDE.md（全域規則）。
reader_codex ：~/.codex/memories/rollout_summaries/*.md（每 thread 結構化記憶）+ ~/.codex/AGENTS.md（全域規則）。

硬性 READ deny-list：~/.codex/memories/ 底下只准讀 rollout_summaries/，
其餘（MEMORY.md / raw_memories.md / memory_summary.md / *.sqlite / extensions/）一律拒讀——
那是 memory_consolidate_global pipeline 領地，且 v5 cron 已把同步污染寫進 MEMORY.md，
讀它就會把污染當「Codex 現況」吸回來（地雷2 + 間接回授）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import _v3mapper as m
import identity as ident

HOME = Path.home()
CLAUDE_ROOT = HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_ROOT / "projects"
CLAUDE_GLOBAL_RULES = CLAUDE_ROOT / "CLAUDE.md"

CODEX_MEM = HOME / ".codex" / "memories"
ROLLOUT_DIR = CODEX_MEM / "rollout_summaries"
CODEX_GLOBAL_AGENTS = HOME / ".codex" / "AGENTS.md"

_HEADER_RE = re.compile(r"^(thread_id|updated_at|cwd|git_branch|rollout_path):\s*(.*)$")


@dataclass
class RawItem:
    side: str            # 'claude' | 'codex'
    kind: str            # 'memory' | 'rule'
    origin_path: str
    logical_id: str
    title: object = None
    marker_root: object = None
    method: str = ""
    cwd: object = None
    disposable: bool = False
    meta: bool = False
    extra: dict = field(default_factory=dict)


def _assert_not_denied(path: Path) -> None:
    """硬閘：拒讀 ~/.codex/memories/ 底下非 rollout_summaries 的任何檔。"""
    rp = path.resolve()
    mem = CODEX_MEM.resolve()
    if str(rp).startswith(str(mem)) and not str(rp).startswith(str(ROLLOUT_DIR.resolve())):
        raise RuntimeError(f"READ deny-list 違反：拒讀 pipeline 檔 {rp}")


def read_claude(project_map: dict) -> list:
    items: list = []
    evidence = m.parse_claude_jsonl_evidence([CLAUDE_ROOT], max_jsonl_lines=500)

    # 全域規則
    if CLAUDE_GLOBAL_RULES.exists():
        items.append(RawItem("claude", "rule", str(CLAUDE_GLOBAL_RULES),
                             ident.GLOBAL_ID, "（全域規則）", None, "global", None))

    # 每專案記憶（只走 projects/，避開 telemetry/sessions/backups 等雜訊）
    if CLAUDE_PROJECTS.exists():
        for it in m.collect_source_items([CLAUDE_PROJECTS], max_files=20000):
            _assert_not_denied(it.path)
            if it.kind != "memory":
                continue
            cpd = it.claude_project_dir.resolve()
            ev = evidence.get(cpd)
            cwd = ev.cwd_counts.most_common(1)[0][0] if (ev and ev.cwd_counts) else None
            if cwd:
                root, remotes = ident.resolve_cwd(cwd)
                lid, title, method = ident.logical_id(root, remotes, project_map)
                disp = ident.is_disposable(cwd, project_map)
                meta = ident.is_meta(cwd)
            else:
                root, lid, title, method, disp, meta = None, ident.UNASSIGNED, None, "no_cwd", False, False
            items.append(RawItem("claude", "memory", str(it.path), lid, title,
                                 str(root) if root else None, method, cwd, disp, meta,
                                 {"claude_project_dir": str(cpd)}))
    return items


def read_codex(project_map: dict) -> list:
    items: list = []

    # 全域規則
    if CODEX_GLOBAL_AGENTS.exists():
        items.append(RawItem("codex", "rule", str(CODEX_GLOBAL_AGENTS),
                             ident.GLOBAL_ID, "（全域規則）", None, "global", None))

    if not ROLLOUT_DIR.exists():
        return items

    for md in sorted(ROLLOUT_DIR.glob("*.md")):
        _assert_not_denied(md)
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        head: dict = {}
        for line in text.splitlines()[:15]:
            mm = _HEADER_RE.match(line)
            if mm:
                head[mm.group(1)] = mm.group(2).strip()
        cwd = head.get("cwd")
        if cwd:
            root, remotes = ident.resolve_cwd(cwd)
            lid, title, method = ident.logical_id(root, remotes, project_map)
            disp = ident.is_disposable(cwd, project_map)
            meta = ident.is_meta(cwd, md.name)
        else:
            root, lid, title, method, disp, meta = None, ident.UNASSIGNED, None, "no_cwd", False, False
        items.append(RawItem("codex", "memory", str(md), lid, title,
                             str(root) if root else None, method, cwd, disp, meta,
                             {"git_branch": head.get("git_branch"), "thread_id": head.get("thread_id")}))
    return items
