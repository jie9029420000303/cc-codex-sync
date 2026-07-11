"""技能層同步：SKILL.md 資料夾整包雙向鏡像（generation 新疊舊、.memsync-origin 防回授）。

同步單位＝含 SKILL.md 的資料夾整包（SKILL.md＋附帶 scripts/references），不做內容揉合。
兩側同格式（agentskills.io 開放標準），鏡像即同步。

同步對（pair）：
  全域    ~/.claude/skills/  ↔  ~/.codex/skills/
  專案級  <repo>/.claude/skills/  ↔  <repo>/.codex/skills/（repo 來自 project_map match_cwds ＋ scan 根）

防回授：memsync 寫出的鏡像資料夾內放 `.memsync-origin`（JSON：origin_side / content_sha256 / generation）。
採集時帶標記且 hash 未變者視為「我們的鏡像」不算來源；hash 變了＝使用者真的改過 → 升級為來源（新疊舊）。
勝出側的舊標記會被移除（它成為新的人工來源）。

衝突（兩側都是來源且內容不同）：與 hub 基準（canonical）不同者＝新 → 新疊舊；
首輪無基準時以「資料夾內最新檔案 mtime」較新者勝，並在 plan 印出衝突警示。

殭屍：來源側資料夾被刪、對側只剩未改動的鏡像 → canonical 與鏡像一併刪除（立即、無寬限期）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from normalize import slug as _slug, id_dir as _id_dir

ORIGIN_FILE = ".memsync-origin"
GLOBAL_SCOPE = "__global__"

# 技能檔內出現這些樣式時警示（平台硬編路徑，鏡像過去可能失效）
_PATH_WARN_RE = re.compile(r"(~/\.claude/|~/\.codex/|/Users/[^\s\"']+|/home/[^\s\"']+)")


def claude_global_root() -> Path:
    return Path.home() / ".claude" / "skills"


def codex_global_root() -> Path:
    return Path.home() / ".codex" / "skills"


# ---------- 掃描 ----------

def folder_hash(d: Path) -> str:
    """整包內容 hash：排序後的相對路徑＋檔案 bytes（排除 .memsync-origin）。"""
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file() and p.name != ORIGIN_FILE):
        h.update(str(f.relative_to(d)).encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(f.read_bytes())
        except Exception:
            pass
        h.update(b"\x00")
    return h.hexdigest()


def folder_mtime(d: Path) -> float:
    mts = [p.stat().st_mtime for p in d.rglob("*") if p.is_file() and p.name != ORIGIN_FILE]
    return max(mts, default=0.0)


def read_origin(d: Path):
    f = d / ORIGIN_FILE
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def scan_root(root: Path) -> dict:
    """root 底下所有含 SKILL.md 的資料夾 → {技能名: {path,hash,origin,mtime,is_mirror}}。
    is_mirror＝帶 .memsync-origin 且記錄的 hash 與現況一致（我們放的、沒被人改過）。"""
    out: dict = {}
    if not root.exists():
        return out
    for sk in sorted(root.rglob("SKILL.md")):
        d = sk.parent
        name = str(d.relative_to(root))
        h = folder_hash(d)
        origin = read_origin(d)
        out[name] = {
            "path": d, "hash": h, "origin": origin, "mtime": folder_mtime(d),
            "is_mirror": bool(origin and origin.get("content_sha256") == h),
        }
    return out


def path_warnings(d: Path) -> list:
    """技能檔含硬編路徑 → 警示行（不擋、只提醒）。"""
    warns = []
    for f in sorted(p for p in d.rglob("*") if p.is_file() and p.name != ORIGIN_FILE):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits = sorted(set(_PATH_WARN_RE.findall(text)))
        if hits:
            warns.append(f"⚠ {f.name} 內含硬編路徑 {hits[:3]}（鏡像到對側可能失效）")
    return warns


# ---------- 同步對 ----------

def sync_pairs(pm: dict, extra_roots=()) -> list:
    """回 [(scope_id, claude_dir, codex_dir)]。全域一對＋每個專案根一對。"""
    pairs = [(GLOBAL_SCOPE, claude_global_root(), codex_global_root())]
    roots = set()
    for proj in pm.get("projects", []):
        for cwd in proj.get("match_cwds", []):
            p = Path(cwd).expanduser()
            if p.is_dir():
                roots.add(p.resolve())
    for r in extra_roots:
        p = Path(r)
        if p.is_dir():
            roots.add(p.resolve())
    for root in sorted(roots):
        c, x = root / ".claude" / "skills", root / ".codex" / "skills"
        if c.exists() or x.exists():
            pairs.append((f"proj__{_slug(root.name)}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}", c, x))
    return pairs


# ---------- canonical ----------

def canonical_skill_dir(canonical_root: Path, scope: str, name: str) -> Path:
    return canonical_root / "__skills__" / _id_dir(scope) / _slug(name)


def load_canonical_meta(cdir: Path):
    f = cdir / "meta.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_canonical(cdir: Path, src: Path, meta: dict) -> None:
    files = cdir / "files"
    if files.exists():
        shutil.rmtree(files)
    files.mkdir(parents=True, exist_ok=True)
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.name != ORIGIN_FILE):
        dst = files / f.relative_to(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
    (cdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 裁決＋ops ----------

def _classify(info):
    """回 'absent' | 'mirror' | 'source'。"""
    if info is None:
        return "absent"
    return "mirror" if info["is_mirror"] else "source"


def merge_pair(canonical_root: Path, scope: str, c_skills: dict, x_skills: dict, generation: int):
    """一對目錄的全部技能裁決。回 (decisions, conflicts)：
    decisions = {name: {"winner": 'claude'|'codex'|None, "action": 'adopt'|'update'|'unchanged'|'zombie'}}"""
    decisions, conflicts = {}, []
    names = set(c_skills) | set(x_skills)
    scope_dir = canonical_root / "__skills__" / _id_dir(scope)
    if scope_dir.exists():
        for d in scope_dir.iterdir():
            meta = load_canonical_meta(d) if d.is_dir() else None
            if meta and meta.get("name"):
                names.add(meta["name"])

    for name in sorted(names):
        c, x = c_skills.get(name), x_skills.get(name)
        cdir = canonical_skill_dir(canonical_root, scope, name)
        can = load_canonical_meta(cdir)
        kc, kx = _classify(c), _classify(x)

        sources = [(s, i) for s, i, k in (("claude", c, kc), ("codex", x, kx)) if k == "source"]

        if not sources:
            # 沒有任何人工來源：鏡像是孤兒（來源已刪）→ 殭屍
            if can or kc == "mirror" or kx == "mirror":
                decisions[name] = {"winner": None, "action": "zombie", "can": can}
            continue

        if len(sources) == 1:
            side, info = sources[0]
            if can and can.get("content_sha256") == info["hash"]:
                decisions[name] = {"winner": side, "action": "unchanged", "info": info, "can": can}
            else:
                decisions[name] = {"winner": side,
                                   "action": "adopt" if not can else "update", "info": info, "can": can}
            continue

        # 兩側都是來源
        (sa, ia), (sb, ib) = sources
        if ia["hash"] == ib["hash"]:
            act = "unchanged" if (can and can.get("content_sha256") == ia["hash"]) else ("adopt" if not can else "update")
            decisions[name] = {"winner": sa, "action": act, "info": ia, "can": can, "both_same": True}
            continue
        if can:
            a_new, b_new = ia["hash"] != can.get("content_sha256"), ib["hash"] != can.get("content_sha256")
            if a_new and not b_new:
                decisions[name] = {"winner": sa, "action": "update", "info": ia, "can": can}
                continue
            if b_new and not a_new:
                decisions[name] = {"winner": sb, "action": "update", "info": ib, "can": can}
                continue
        # 首輪衝突或兩側皆新：資料夾 mtime 較新者勝
        win_side, win_info = (sa, ia) if ia["mtime"] >= ib["mtime"] else (sb, ib)
        conflicts.append(f"⚠ 技能衝突 [{scope}/{name}]：兩側都有修改，以較新側 {win_side} 整包勝出（另一側將被覆蓋）")
        decisions[name] = {"winner": win_side, "action": "adopt" if not can else "update",
                           "info": win_info, "can": can}
    return decisions, conflicts


def _copy_skill(src: Path, dst: Path, marker: dict) -> None:
    """整包覆蓋寫入 dst ＋ 溯源標記。"""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.name != ORIGIN_FILE):
        t = dst / f.relative_to(src)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
    (dst / ORIGIN_FILE).write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")


def build_ops(canonical_root: Path, scope: str, c_root: Path, x_root: Path,
              decisions: dict, c_skills: dict, x_skills: dict, generation: int, ts: str):
    """把裁決轉成可執行 ops。回 (ops, warns)。op：
    {action: mirror|remove_marker|delete|canonical|canonical_delete|unchanged, target, name, src?, marker?}"""
    ops, warns = [], []
    for name, d in sorted(decisions.items()):
        cdir = canonical_skill_dir(canonical_root, scope, name)
        if d["action"] == "zombie":
            for side, root, infos in (("claude", c_root, c_skills), ("codex", x_root, x_skills)):
                info = infos.get(name)
                if info is not None:
                    ops.append({"action": "delete", "target": str(info["path"]), "name": name, "side": side})
            if load_canonical_meta(cdir):
                ops.append({"action": "canonical_delete", "target": str(cdir), "name": name})
            continue

        win, info = d["winner"], d.get("info")
        if d["action"] in ("adopt", "update"):
            first = (d.get("can") or {}).get("first_seen_generation", generation)
            ops.append({"action": "canonical", "target": str(cdir), "name": name, "src": str(info["path"]),
                        "meta": {"name": name, "scope": scope, "origin_side": win,
                                 "content_sha256": info["hash"],
                                 "first_seen_generation": first, "last_seen_generation": generation}})
            warns.extend(f"  [{name}] {w}" for w in path_warnings(info["path"]))

        can_meta = d.get("can") or {}
        target_hash = info["hash"] if info else can_meta.get("content_sha256")
        # 勝出側若殘留舊標記（被使用者改過的前鏡像）→ 移除標記，它已是人工來源
        win_info = (c_skills if win == "claude" else x_skills).get(name)
        if win_info and win_info["origin"] and not win_info["is_mirror"]:
            ops.append({"action": "remove_marker", "target": str(win_info["path"] / ORIGIN_FILE), "name": name})
        # 敗側／缺側 → 鏡像成勝出版
        for side, root, infos in (("claude", c_root, c_skills), ("codex", x_root, x_skills)):
            if side == win:
                continue
            other = infos.get(name)
            if other is not None and other["hash"] == target_hash and other["is_mirror"]:
                continue  # 鏡像已是最新
            if other is not None and other["hash"] == target_hash and other["origin"] is None:
                continue  # 兩側人工內容相同，不動也不貼標
            src = info["path"] if info else (cdir / "files")
            marker = {"origin": "memsync", "origin_side": win, "content_sha256": target_hash,
                      "generation": generation, "synced_at": ts}
            ops.append({"action": "mirror", "target": str(root / name), "name": name,
                        "src": str(src), "marker": marker, "side": side})
    return ops, warns


def apply_ops(ops: list) -> int:
    n = 0
    for o in ops:
        t = Path(o["target"])
        if o["action"] == "mirror":
            _copy_skill(Path(o["src"]), t, o["marker"]); n += 1
        elif o["action"] == "canonical":
            save_canonical(t, Path(o["src"]), o["meta"]); n += 1
        elif o["action"] == "delete":
            if t.exists():
                shutil.rmtree(t); n += 1
        elif o["action"] == "canonical_delete":
            if t.exists():
                shutil.rmtree(t); n += 1
        elif o["action"] == "remove_marker":
            t.unlink(missing_ok=True); n += 1
    return n
