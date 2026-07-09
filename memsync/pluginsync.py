"""Plugin 拆解掃描（唯讀）：不搬殼，只盤點兩側 plugin 內含的技能／MCP 與對側覆蓋狀況。

兩家 plugin 打包格式不同且都在演進，整包搬運脆且難維護；共享的真值是內容物
（SKILL.md 技能、MCP server 定義）。此指令只產報告，實際搬運交給 skills / mcp 管線。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # 3.9/3.10：本模組純唯讀報告，缺 tomllib 時跳過 config.toml 盤點即可
    tomllib = None


def plugin_roots():
    return [("claude", Path.home() / ".claude" / "plugins"),
            ("codex", Path.home() / ".codex" / "plugins")]


def _plugin_dirs(root: Path):
    """root 下第一層（或 repos/<org>/<repo> 第三層）視為一顆 plugin。"""
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name in ("repos", "marketplaces", "cache"):
            for sub in sorted(p for p in d.rglob("*") if p.is_dir() and (
                    (p / "SKILL.md").exists() or (p / ".mcp.json").exists()
                    or (p / "plugin.json").exists() or (p / "skills").is_dir())):
                out.append(sub)
        else:
            out.append(d)
    seen, uniq = set(), []
    for d in out:
        if not any(str(d).startswith(str(s) + "/") for s in seen):
            seen.add(d)
            uniq.append(d)
    return uniq


def scan_plugin(d: Path):
    """回 {skills:[名], mcp:[server名]}。防禦式解析，讀不懂就略過。"""
    skills = sorted({sk.parent.name for sk in d.rglob("SKILL.md")})
    mcp = set()
    for f in list(d.rglob(".mcp.json")) + list(d.rglob("mcp.json")):
        try:
            mcp |= set((json.loads(f.read_text(encoding="utf-8")).get("mcpServers") or {}).keys())
        except Exception:
            pass
    for f in d.rglob("config.toml"):
        if tomllib is None:
            break
        try:
            mcp |= set((tomllib.loads(f.read_text(encoding="utf-8")).get("mcp_servers") or {}).keys())
        except Exception:
            pass
    return {"skills": skills, "mcp": sorted(mcp)}


def installed_skills(side: str) -> set:
    root = Path.home() / (".claude" if side == "claude" else ".codex") / "skills"
    if not root.exists():
        return set()
    return {sk.parent.name for sk in root.rglob("SKILL.md")}


def report(claude_servers: dict, codex_servers: dict) -> list:
    """產出報告行。claude_servers/codex_servers＝兩側目前已設定的 MCP server 名集合。"""
    lines = ["# plugins 拆解盤點（唯讀，不搬殼）"]
    any_found = False
    for side, root in plugin_roots():
        other = "codex" if side == "claude" else "claude"
        other_sk = installed_skills(other)
        other_mcp = codex_servers if side == "claude" else claude_servers
        dirs = _plugin_dirs(root)
        lines.append(f"\n## {side} 側 plugins（{root}）：{len(dirs)} 顆")
        if not dirs:
            lines.append("- （無）")
            continue
        for d in dirs:
            info = scan_plugin(d)
            if not info["skills"] and not info["mcp"]:
                continue
            any_found = True
            lines.append(f"- **{d.name}**")
            for s in info["skills"]:
                mark = "✓ 對側已有" if s in other_sk else f"✗ {other} 側缺 → ./msync skills"
                lines.append(f"    - 技能 `{s}` … {mark}")
            for m in info["mcp"]:
                mark = "✓ 對側已有" if m in other_mcp else f"✗ {other} 側缺 → ./msync mcp"
                lines.append(f"    - MCP `{m}` … {mark}")
    if not any_found:
        lines.append("\n（兩側 plugin 內未發現可拆解的技能／MCP。）")
    lines.append("\n→ 缺的項目：技能跑 ./msync skills、MCP 跑 ./msync mcp（plugin 殼不搬，各工具自行管理安裝）")
    return lines
