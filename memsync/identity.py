"""Logical project identity layer — wraps the vendored v3 mapper.

把「磁碟上真實的 cwd / marker-root」收斂成一個穩定的 logical_project_id，套用：
- 人工 override（project_map.json）：最高權威，解「同一專案在兩邊是不同路徑/不同 repo」
- git-remote 正規化：同一 repo 的兩個 checkout => 同一 id
- path fallback：以 realpath 為 key（永不靠 basename 誤併）
- disposable 過濾：Codex ~/Documents/Codex/<date>/<slug> 拋棄式暫存夾
- meta-feedback 過濾：同步器談論自己（防二階語意回授）

本檔只「決定身份」，不讀檔、不寫檔。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import _v3mapper as m

HOME = Path.home()

# Codex 桌面版每個 session 的拋棄式暫存夾樣式
DISPOSABLE_CWD_RE = re.compile(r"/Documents/Codex/\d{4}-\d{2}-\d{2}/")
# 同步器自身（防「工具談論工具自己」的二階回授）
META_PAT = re.compile(
    r"(memsync|memhub|sync[-_ ]?cloud[-_ ]?code|sync[-_ ]?claude[-_ ]?code"
    r"|cloud-code-cloud-code|多ai工具記憶與規則同步)",
    re.IGNORECASE,
)

GLOBAL_ID = "__GLOBAL__"
UNASSIGNED = "__UNASSIGNED__"


def load_project_map(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _norm_path(value) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return str(value)


def resolve_cwd(cwd: str):
    """cwd 字串 -> (marker_root: Path|None, remotes: tuple)。純讀路徑，不改任何東西。"""
    p = Path(cwd).expanduser()
    existing = m.nearest_existing_path(p)
    if not existing:
        return None, ()
    root = m.nearest_marker_root(existing, [])
    _git_root, remotes = m.read_git_remotes(existing)
    return root, remotes


def logical_id(marker_root, remotes, project_map: dict):
    """-> (id, title|None, method)。

    優先序：override > git_remote > path(realpath) > UNASSIGNED。
    """
    projects = project_map.get("projects", [])
    rp = _norm_path(marker_root) if marker_root else None
    norm_remotes = {m.normalize_git_url(r) for r in (remotes or ())}

    # 1) 人工 override（最高權威）
    for proj in projects:
        pid = proj.get("id")
        if not pid:
            continue
        omr = {m.normalize_git_url(r) for r in proj.get("match_remotes", [])}
        ocw = {_norm_path(x) for x in (proj.get("match_cwds", []) + proj.get("match_paths", []))}
        if (norm_remotes & omr) or (rp and rp in ocw):
            return pid, proj.get("title", pid), "override"

    # 2) git remote 正規化（同 repo 兩 checkout 自動併）
    if norm_remotes:
        return "remote:" + sorted(norm_remotes)[0], None, "git_remote"

    # 3) path fallback（以 realpath 為 key，不靠 basename）
    if rp:
        return "path:" + rp, None, "path"

    return UNASSIGNED, None, "unresolved"


def is_disposable(cwd, project_map: dict) -> bool:
    if not cwd:
        return False
    allow = {_norm_path(x) for x in project_map.get("disposable_allow", [])}
    if _norm_path(cwd) in allow:
        return False
    if DISPOSABLE_CWD_RE.search(str(cwd)):
        return True
    for pat in project_map.get("disposable_extra", []):
        try:
            if re.search(pat, str(cwd)):
                return True
        except re.error:
            continue
    return False


def is_meta(*texts) -> bool:
    return any(t and META_PAT.search(str(t)) for t in texts)
