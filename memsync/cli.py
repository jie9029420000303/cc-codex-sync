#!/usr/bin/env python3
"""memsync — Codex <-> Claude Code 記憶/規則 雙向同步器。

P1 已實作（唯讀，不寫任何工具檔）：
  scan   跨兩側採集 + 對映，印出「哪些記憶/規則屬哪個邏輯專案」報告
  map    產/列 project_map.json 人工對照骨架 + suspected-pair 待確認清單
  status 印 hub 現況

P2+ 佔位（尚未實作寫回）：plan / diff / apply / verify / rollback
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _v3mapper as m       # noqa: E402
import identity as ident    # noqa: E402
import readers              # noqa: E402
import normalize as norm    # noqa: E402
import blocks               # noqa: E402
import store                # noqa: E402
import rules                # noqa: E402
import integrate as integ   # noqa: E402
import compat               # noqa: E402
import skillsync            # noqa: E402
import mcpsync              # noqa: E402
import pluginsync           # noqa: E402

# hub＝canonical/state/project_map 的家。
# 從原始碼跑＝repo 目錄（維持既有行為）；打包成單檔執行檔跑＝~/.cc-codex-sync
# （PyInstaller 的 __file__ 在解壓暫存區，不能當資料目錄）。可用環境變數覆蓋。
if os.environ.get("CC_CODEX_SYNC_HOME"):
    HUB = Path(os.environ["CC_CODEX_SYNC_HOME"]).expanduser().resolve()
elif getattr(sys, "frozen", False):
    HUB = Path.home() / ".cc-codex-sync"
else:
    HUB = Path(__file__).resolve().parent.parent
HUB.mkdir(parents=True, exist_ok=True)
PROJECT_MAP = HUB / "project_map.json"
CANONICAL = HUB / "canonical"
RUNS = HUB / "runs"
STATE = HUB / "state.json"


def _collect(pm):
    return readers.read_claude(pm) + readers.read_codex(pm)


def _bucketize(items):
    buckets = defaultdict(lambda: {
        "title": None, "methods": set(), "roots": set(),
        "claude_mem": 0, "codex_mem": 0, "claude_rule": 0, "codex_rule": 0,
    })
    disposable, meta = [], []
    for it in items:
        if it.meta:
            meta.append(it)
            continue
        if it.disposable:
            disposable.append(it)
            continue
        b = buckets[it.logical_id]
        if it.title and not b["title"]:
            b["title"] = it.title
        b["methods"].add(it.method)
        if it.marker_root:
            b["roots"].add(it.marker_root)
        b[f"{it.side}_{'rule' if it.kind == 'rule' else 'mem'}"] += 1
    return buckets, disposable, meta


def _short(p):
    return str(p).replace(str(Path.home()), "~")


def _suspected_pairs(buckets):
    """token 相似卻落在不同 id 的桶 → 提示人工建 override。

    從『單側桶』出發，與『所有真實桶（含兩側桶）』比對：這樣某側純本地的
    checkout 才能對上已在兩側桶的同名 repo。只是提示、不自動併。
    """
    def toks_of(b):
        t = set()
        for r in b["roots"]:
            t |= m.tokenize(Path(r).name)
        return t

    def both(b):
        return (b["claude_mem"] + b["claude_rule"] > 0) and (b["codex_mem"] + b["codex_rule"] > 0)

    real = [(lid, b, toks_of(b)) for lid, b in buckets.items()
            if lid not in (ident.GLOBAL_ID, ident.UNASSIGNED)]
    pairs, seen = [], set()
    for lid, b, tk in real:
        if both(b):
            continue  # 只從單側桶當種子
        for lid2, b2, tk2 in real:
            if lid2 == lid:
                continue
            key = tuple(sorted((lid, lid2)))
            if key in seen:
                continue
            overlap = tk & tk2
            if overlap:
                seen.add(key)
                pairs.append((lid, lid2, sorted(overlap), b, b2))
    pairs.sort(key=lambda x: -len(x[2]))
    return pairs


def cmd_scan(args):
    pm = ident.load_project_map(PROJECT_MAP)
    items = _collect(pm)
    buckets, disposable, meta = _bucketize(items)

    real = {k: v for k, v in buckets.items() if k not in (ident.GLOBAL_ID, ident.UNASSIGNED)}
    both_sides = {k: v for k, v in real.items()
                  if (v["claude_mem"] + v["claude_rule"] > 0) and (v["codex_mem"] + v["codex_rule"] > 0)}
    claude_only = {k: v for k, v in real.items() if (v["codex_mem"] + v["codex_rule"] == 0)}
    codex_only = {k: v for k, v in real.items() if (v["claude_mem"] + v["claude_rule"] == 0)}
    pairs = _suspected_pairs(buckets)

    lines = []
    p = lines.append
    p("# memsync scan 報告（唯讀，未寫任何工具檔）")
    p(f"\n- 產生時間：{datetime.now().isoformat(timespec='seconds')}")
    p(f"- 採集：Claude {sum(1 for i in items if i.side=='claude')} 項 · Codex {sum(1 for i in items if i.side=='codex')} 項")
    p(f"- 邏輯專案：{len(real)}（兩側都有 {len(both_sides)} · 僅Claude {len(claude_only)} · 僅Codex {len(codex_only)}）")
    p(f"- 已濾除：拋棄夾 {len(disposable)} · 同步器自身(meta) {len(meta)}")

    g = buckets.get(ident.GLOBAL_ID)
    if g:
        p(f"\n## 全域規則（global ↔ global）\n- Claude CLAUDE.md：{g['claude_rule']} · Codex AGENTS.md：{g['codex_rule']}（直接對映）")

    def table(title, bk):
        p(f"\n## {title}（{len(bk)}）")
        if not bk:
            p("- （無）")
            return
        p("\n| 邏輯專案 id | Claude記憶 | Codex記憶 | 對映法 | 解析路徑 |")
        p("|---|---|---|---|---|")
        for lid, b in sorted(bk.items(), key=lambda kv: -(kv[1]['claude_mem']+kv[1]['codex_mem'])):
            root = _short(sorted(b["roots"])[0]) if b["roots"] else "—"
            label = b["title"] or lid.replace("remote:", "git:").replace("path:", "")
            p(f"| `{_short(label)[:42]}` | {b['claude_mem']} | {b['codex_mem']} | {'/'.join(sorted(b['methods']))} | {root[:48]} |")

    table("★ 兩側都有 — 需雙向揉合", both_sides)
    table("僅 Claude 有", claude_only)
    table("僅 Codex 有", codex_only)

    if pairs:
        p(f"\n## ⚠ 疑似同源、待人工建 override（{len(pairs)}）")
        p("這些是 token 相似卻被算成不同 id 的單側專案——很可能是同一邏輯專案在兩邊不同路徑/repo。")
        for a, b, ov, ba, bb in pairs[:12]:
            ra = _short(sorted(ba['roots'])[0]) if ba['roots'] else a
            rb = _short(sorted(bb['roots'])[0]) if bb['roots'] else b
            p(f"- 共同 token `{'/'.join(ov)}`：\n    - {ra}\n    - {rb}\n    → 若同源，在 project_map.json 用一個 id 把兩個路徑/remote 列進 match_cwds/match_remotes")

    un = buckets.get(ident.UNASSIGNED)
    if un and (un["claude_mem"] + un["codex_mem"]):
        p(f"\n## 未對映 _unassigned（Claude {un['claude_mem']} · Codex {un['codex_mem']}）\n- cwd 不存在或無證據，暫不寫回，待人工認領。")

    report = "\n".join(lines)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = RUNS / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scan-report.md").write_text(report, encoding="utf-8")
    (run_dir / "scan.json").write_text(json.dumps([it.__dict__ for it in items], ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    print(f"\n[已存] {_short(run_dir / 'scan-report.md')}")
    return 0


def cmd_map(args):
    pm = ident.load_project_map(PROJECT_MAP)
    if not PROJECT_MAP.exists():
        template = {
            "_comment": "人工專案身份對照表。把『同一邏輯專案在兩邊的不同路徑/不同 git remote』用一個 id 收斂。memsync 對映優先序：override > git_remote > path。",
            "projects": [],
            "_examples": [{
                "id": "example-project", "title": "範例：官網專案",
                "match_remotes": ["https://github.com/your-org/example-repo"],
                "match_cwds": ["/Users/you/Documents/example-project"],
            }],
            "disposable_allow": [],
            "disposable_extra": [],
        }
        PROJECT_MAP.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已建立 project_map.json 骨架：{_short(PROJECT_MAP)}")
    else:
        print(f"project_map.json 已存在（{len(pm.get('projects', []))} 條 override）：{_short(PROJECT_MAP)}")
    print("→ 跑 `memsync scan` 看 suspected-pair，再把同源專案填進 projects[]。")
    return 0


def cmd_status(args):
    pm = ident.load_project_map(PROJECT_MAP)
    state = _load_state()
    print(f"hub: {_short(HUB)}")
    print(f"generation: {state.get('generation', 0)}")
    print(f"project_map.json: {len(pm.get('projects', []))} 條 override")
    print(f"runs/: {len(list(RUNS.glob('*'))) if RUNS.exists() else 0} 次掃描")
    if CANONICAL.exists():
        projs = [d for d in CANONICAL.iterdir() if d.is_dir() and not d.name.startswith("__")]
        print(f"canonical/（中央倉）: {len(projs)} 個專案")
        for d in sorted(projs):
            ents = store.load_canonical_entries(CANONICAL, d.name)
            by = defaultdict(int)
            for e in ents:
                by[e.origin_side] += 1
            print(f"  - {d.name}: {len(ents)} 顆 {dict(by)}")
        sk = CANONICAL / "__skills__"
        if sk.exists():
            n = sum(1 for _ in sk.glob("*/*/meta.json"))
            print(f"技能層 __skills__: {n} 顆")
        mc = CANONICAL / "__mcp__"
        if mc.exists():
            print(f"MCP 層 __mcp__: {sum(1 for _ in mc.glob('*.json'))} 顆 server")
    else:
        print("canonical/（中央倉）: 空（先 ./msync collect）")
    return 0


def _load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"generation": 0, "targets": {}}


def _collect_into_canonical(pm, project, generation):
    """讀兩側來源 → normalize → 寫進 canonical/<project>/entries/。

    每次呼叫都是該 project 的完整權威快照：本輪沒被任何來源提到的既有 entry 視為
    來源已刪除/搬移，由 store.merge_into_canonical 立即清除（無寬限期）。
    回 (本輪 entries, added, updated, unchanged, removed)。"""
    raws = [it for it in _collect(pm) if it.logical_id == project and it.kind == "memory"]
    entries = []
    for it in raws:
        e = norm.entry_from_claude_memory(it) if it.side == "claude" else norm.entry_from_codex_rollout(it)
        if e:
            entries.append(e)
    a, u, n, r = store.merge_into_canonical(CANONICAL, project, entries, generation)
    return entries, a, u, n, r


def _doc_block_op(path, text, kind, project, label, ts, typ):
    """把一整份整合好的 markdown 包成受管 block 寫回。hash 以整段文字算。"""
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new_h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    ok, reason = blocks.integrity_check(old, kind, project)
    base = {"target": str(path), "typ": typ, "kind": kind, "project": project, "hash": new_h, "reason": "", "warn": ""}
    if not ok:
        return {**base, "action": "BLOCKED", "reason": reason, "old": old, "new": old}
    if blocks.existing_hash(old, kind, project) == new_h:
        return {**base, "action": "unchanged", "old": old, "new": old}
    nb = blocks.render_doc_block(text, kind, project, label, ts)
    new = blocks.upsert(old, nb, kind, project)
    return {**base, "action": ("create" if not path.exists() else "update"), "old": old, "new": new,
            "warn": blocks.growth_warn(old, new, kind, project)}


def _integrated_granule_op(path, text, project):
    new = blocks.render_integrated_granule(text, project)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    action = "unchanged" if old == new else ("create" if not path.exists() else "update")
    h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    return {"target": str(path), "typ": "granule", "kind": "granule", "project": project,
            "hash": h, "reason": "", "warn": "", "action": action, "old": old, "new": new}


def _index_op(path, project, ts):
    kind = "memory-index"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new_h = hashlib.sha256(("memsync-integrated-index:" + project).encode()).hexdigest()[:12]
    ok, reason = blocks.integrity_check(old, kind, project)
    base = {"target": str(path), "typ": "index", "kind": kind, "project": project, "hash": new_h, "reason": "", "warn": ""}
    if not ok:
        return {**base, "action": "BLOCKED", "reason": reason, "old": old, "new": old}
    if blocks.existing_hash(old, kind, project) == new_h:
        return {**base, "action": "unchanged", "old": old, "new": old}
    nb = blocks.render_integrated_index(project, ts)
    new = blocks.upsert(old, nb, kind, project)
    return {**base, "action": ("create" if not path.exists() else "update"), "old": old, "new": new}


def _ensure_integrated(pm, project, generation):
    """輸入(entries)有變或還沒整合過 → 呼叫 LLM 整合並存 canonical/<project>/integrated.md。
    回 (整合後文字 or None, changed:bool)。輸入沒變＝沿用既有整合版（冪等、不重跑 LLM）。
    整合失敗時保守不覆蓋既有版。"""
    entries = store.load_canonical_entries(CANONICAL, project)
    if not entries:
        return None, False
    in_hash = store.input_hash(entries)
    prev_text, prev_hash = store.load_integrated(CANONICAL, project)
    if prev_text and prev_hash == in_hash:
        return prev_text, False
    text = integ.integrate_memory(entries, previous_integrated=prev_text)
    if not text:
        return prev_text, False
    store.save_integrated(CANONICAL, project, text, in_hash, generation)
    return text, True


def _build_ops(pm, project):
    """把該專案的『整合版』寫回兩側同一份（不再互補投影）：
    Codex 側 AGENTS.md 受管 block、Claude 側一顆整合 granule ＋ MEMORY.md 索引。
    只讀 canonical/<project>/integrated.md（整合須先由 _ensure_integrated 產生）。
    孤兒清除：舊的 per-entry memsync-*.md 顆粒（改用整合版單檔後不再需要）一併刪除。"""
    integrated, _ = store.load_integrated(CANONICAL, project)
    entries = store.load_canonical_entries(CANONICAL, project)
    raws = [it for it in _collect(pm) if it.logical_id == project]
    codex_roots = sorted({it.marker_root for it in raws if it.side == "codex" and it.marker_root})
    claude_roots = sorted({it.marker_root for it in raws if it.side == "claude" and it.marker_root})
    claude_proj_dirs = sorted({it.extra.get("claude_project_dir") for it in raws
                               if it.side == "claude" and it.extra.get("claude_project_dir")})
    ts = datetime.now().isoformat(timespec="seconds")
    ops = []
    current_granule_targets = set()
    if integrated:
        for root in (codex_roots or claude_roots):
            ops.append(_doc_block_op(Path(root) / "AGENTS.md", integrated, "memory", project,
                                     "整合自 Claude Code + Codex", ts, "agents"))
        for pdir in claude_proj_dirs:
            mem = Path(pdir) / "memory"
            gtarget = mem / f"memsync-{store._slug(project)}-integrated.md"
            current_granule_targets.add(str(gtarget))
            ops.append(_integrated_granule_op(gtarget, integrated, project))
            ops.append(_index_op(mem / "MEMORY.md", project, ts))
    state = _load_state()
    for target, meta in state.get("targets", {}).items():
        if meta.get("typ") == "granule" and meta.get("project") == project and target not in current_granule_targets:
            ops.append({"target": target, "typ": "granule", "kind": "granule", "project": project,
                        "hash": "", "reason": "改用整合版單檔，舊 per-entry 顆粒清除", "warn": "", "action": "delete",
                        "old": "", "new": ""})
    return ops, entries, [], []


def cmd_collect(args):
    project = args.project
    pm = ident.load_project_map(PROJECT_MAP)
    state = _load_state()
    gen = state.get("generation", 0) + 1
    entries, a, u, n, r = _collect_into_canonical(pm, project, gen)
    if not entries and not store.load_canonical_entries(CANONICAL, project):
        print(f"專案 `{project}` 無可採集記憶（跑 ./msync scan 看可用 id）")
        return 1
    state["generation"] = gen
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    all_entries = store.load_canonical_entries(CANONICAL, project)
    by_side = defaultdict(int)
    for e in all_entries:
        by_side[e.origin_side] += 1
    print(f"# collect → 中央倉（generation {gen}）")
    print(f"專案 `{project}`：本輪採集 {len(entries)} 顆（新增 {a} · 更新 {u} · 未變 {n} · 清除殭屍 {r}）")
    print(f"canonical/{project}/entries/ 現有 {len(all_entries)} 顆：{dict(by_side)}")
    print(f"→ 下一步：./msync plan --project {project}")
    return 0


_TAG = {"agents": "AGENTS.md", "granule": "memory顆粒", "index": "MEMORY.md索引", "rules": "規則"}


def _write_ops(ops):
    """實寫 create/update/delete ops，更新 state.targets。generation 只由 collect 推進，apply 不動它。"""
    state = _load_state()
    gen = state.get("generation", 0)
    n = 0
    for o in ops:
        if o["action"] == "delete":
            Path(o["target"]).unlink(missing_ok=True)
            state.get("targets", {}).pop(o["target"], None)
            n += 1
            continue
        if o["action"] not in ("create", "update"):
            continue
        p = Path(o["target"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(o["new"], encoding="utf-8")
        state.setdefault("targets", {})[o["target"]] = {
            "typ": o["typ"], "kind": o["kind"], "project": o["project"], "hash": o["hash"],
            "written_at": datetime.now().isoformat(timespec="seconds"), "generation": gen,
        }
        n += 1
    if n:
        state["last_apply"] = datetime.now().isoformat(timespec="seconds")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return n


def _apply_ops(ops, yes, title):
    for b in [o for o in ops if o["action"] == "BLOCKED"]:
        print(f"⛔ 跳過（完整性閘）：{_short(b['target'])} — {b['reason']}")
    for o in ops:
        if o.get("warn"):
            print(f"  {o['warn']} @ {_short(o['target'])}")
    writes = [o for o in ops if o["action"] in ("create", "update", "delete")]
    if not writes:
        print(f"{title}：沒有要寫的變更（全部 unchanged 或 blocked）。")
        return 0
    print(f"{title} 即將寫入/刪除：")
    for o in writes:
        label = "刪除殭屍檔" if o["action"] == "delete" else o["action"]
        print(f"  [{label}] [{_TAG.get(o['typ'], o['typ'])}] {_short(o['target'])}")
    if not yes:
        print("（dry-run；加 --yes 才實寫）")
        return 0
    n = _write_ops(ops)
    print(f"✓ {title} 已處理 {n} 檔。")
    return n


def cmd_plan(args):
    project = args.project
    pm = ident.load_project_map(PROJECT_MAP)
    if not store.load_canonical_entries(CANONICAL, project):
        print(f"中央倉 canonical/{project}/ 是空的——先跑 ./msync collect --project {project}")
        return 1
    print("整合中（輸入有變才呼叫 LLM，可能需數分鐘）…")
    integrated, changed = _ensure_integrated(pm, project, _load_state().get("generation", 0))
    if not integrated:
        print(f"專案 `{project}` 尚無整合版（整合失敗或無來源）。")
        return 1
    print(f"整合版：{'本輪重新整合' if changed else '輸入未變，沿用既有整合版'}（{len(integrated)} 字）")
    ops, entries, _, _ = _build_ops(pm, project)
    if not ops:
        print(f"專案 `{project}` 無寫回目標（缺 Codex 作業根或 Claude 專案目錄）")
        return 1
    print("# plan（中央倉整合版 → 兩側寫回同一份 · dry-run · 未寫任何檔）")
    print(f"專案：{project} · {len(entries)} 顆來源 → 整合成 1 份，兩側同步")
    agg = defaultdict(int)
    for op in ops:
        agg[op["action"]] += 1
    print("動作：" + " · ".join(f"{k} {v}" for k, v in sorted(agg.items())))
    for op in ops:
        print(f"\n## {op['action'].upper()} [{_TAG[op['typ']]}] {_short(op['target'])}")
        if op.get("warn"):
            print(f"  {op['warn']}")
        if op["action"] == "BLOCKED":
            print(f"  ⛔ 完整性硬閘擋下：{op['reason']}（拒寫）")
            continue
        if op["action"] == "unchanged":
            print("  ✓ 一致（hash 相同），跳過")
            continue
        if op["action"] == "delete":
            print(f"  🗑 {op['reason']}，這個 memory 顆粒檔會被刪除")
            continue
        diff = list(difflib.unified_diff(op["old"].splitlines(), op["new"].splitlines(),
                                         fromfile="(現況)", tofile="(套用後)", lineterm=""))
        for line in diff[:40]:
            print("  " + line)
        if len(diff) > 40:
            print(f"  …（diff 共 {len(diff)} 行，截前 40）")
    print(f"\n→ 確認後：./msync apply --project {project} --yes")
    return 0


def cmd_apply(args):
    project = args.project
    pm = ident.load_project_map(PROJECT_MAP)
    if not store.load_canonical_entries(CANONICAL, project):
        print(f"中央倉 canonical/{project}/ 是空的——先跑 ./msync collect --project {project}")
        return 1
    _ensure_integrated(pm, project, _load_state().get("generation", 0))
    ops, *_ = _build_ops(pm, project)
    _apply_ops(ops, args.yes, f"apply {project}")
    return 0


def _mtime(p):
    try:
        return Path(p).stat().st_mtime
    except Exception:
        return 0.0


def _ensure_integrated_rule(scope, c_rule, x_rule, c_mtime, x_mtime):
    """兩側人工規則 LLM 整合成一份（衝突以較新來源檔為準）。input-hash 閘：規則沒變不重跑。"""
    ih = store.rule_input_hash(c_rule or "", x_rule or "")
    prev, prev_h = store.load_integrated_rule(CANONICAL, scope)
    if prev and prev_h == ih:
        return prev
    text = integ.integrate_rules(c_rule, x_rule, previous_integrated=prev,
                                 claude_mtime=c_mtime, codex_mtime=x_mtime)
    if not text:
        return prev
    store.save_integrated_rule(CANONICAL, scope, text, ih)
    return text


def _build_rule_ops(pm):
    """規則層整合：兩側人工規則揉成一份（較新檔勝），兩側寫回同一份整合規則。
    互補投影邏輯保留在『寫哪一側』：只有當對方也有規則、才把整合版寫進這一側（避免單側自我回寫）。"""
    ts = datetime.now().isoformat(timespec="seconds")
    skip = {str(Path(x).expanduser().resolve()) for x in pm.get("rules_skip_targets", [])}
    ops = []

    def emit(path, text, scope):
        if str(Path(path).resolve()) in skip:
            return
        ops.append(_doc_block_op(Path(path), text, "rules", scope, "整合自 Claude Code + Codex 規則", ts, "rules"))

    # 全域（Claude 側人工來源：rules-source.md 優先，不存在時 fallback CLAUDE.md 殘文）
    cr, c_src = rules.claude_global_rule_source()
    xr = rules.human_rule(rules.CODEX_GLOBAL)
    if cr or xr:
        integrated = _ensure_integrated_rule("GLOBAL", cr, xr, _mtime(c_src), _mtime(rules.CODEX_GLOBAL))
        if integrated:
            if cr:
                emit(rules.CODEX_GLOBAL, integrated, "__GLOBAL__")
            # 來源為獨立檔 rules-source.md 時，CLAUDE.md 純屬寫回目標、無自我回寫疑慮，
            # 不需靠「對側也有規則」才投影；fallback 模式（來源仍是 CLAUDE.md 殘文）維持舊條件。
            if xr or (cr and c_src == rules.CLAUDE_RULES_SOURCE):
                emit(rules.CLAUDE_GLOBAL, integrated, "__GLOBAL__")

    # 每專案
    items = _collect(pm)
    for proj in sorted({it.logical_id for it in items if it.logical_id not in (ident.GLOBAL_ID, ident.UNASSIGNED)}):
        raws = [it for it in items if it.logical_id == proj]
        c_roots = sorted({it.marker_root for it in raws if it.side == "claude" and it.marker_root})
        x_roots = sorted({it.marker_root for it in raws if it.side == "codex" and it.marker_root})
        c_files = [Path(x) / "CLAUDE.md" for x in c_roots]
        x_files = [Path(x) / "AGENTS.md" for x in x_roots]
        c_rule = next((r for r in (rules.human_rule(f) for f in c_files) if r), None)
        x_rule = next((r for r in (rules.human_rule(f) for f in x_files) if r), None)
        if not (c_rule or x_rule):
            continue
        c_mt = max([_mtime(f) for f in c_files], default=0.0)
        x_mt = max([_mtime(f) for f in x_files], default=0.0)
        integrated = _ensure_integrated_rule(proj, c_rule, x_rule, c_mt, x_mt)
        if not integrated:
            continue
        if c_rule:
            for x in (x_roots or c_roots):
                emit(Path(x) / "AGENTS.md", integrated, proj)
        if x_rule:
            for x in (c_roots or x_roots):
                emit(Path(x) / "CLAUDE.md", integrated, proj)
    return ops


def cmd_rules(args):
    pm = ident.load_project_map(PROJECT_MAP)
    print("規則整合中（剝除 memsync 區塊後的人工規則 → LLM 揉成一份，衝突以較新來源檔為準；規則有變才呼叫 LLM）…")
    ops = _build_rule_ops(pm)
    if not ops:
        print("沒有可同步的人工規則（兩側規則檔皆空或不存在）。")
        return 0
    print("# rules（規則層整合 · 兩側寫回同一份整合規則 · 較新檔勝）")
    if not args.yes:
        for o in ops:
            print(f"\n## {o['action'].upper()} [{_TAG.get(o['typ'], o['typ'])}] {_short(o['target'])}")
            if o.get("warn"):
                print(f"  {o['warn']}")
            if o["action"] == "BLOCKED":
                print(f"  ⛔ {o['reason']}")
                continue
            if o["action"] == "unchanged":
                print("  ✓ 一致，跳過")
                continue
            diff = list(difflib.unified_diff(o["old"].splitlines(), o["new"].splitlines(),
                                             fromfile="(現況)", tofile="(套用後)", lineterm=""))
            for line in diff[:24]:
                print("  " + line)
            if len(diff) > 24:
                print(f"  …（diff 共 {len(diff)} 行，截前 24）")
        print("\n→ 確認後：./msync rules --yes")
        return 0
    _apply_ops(ops, True, "rules")
    return 0


def cmd_sync(args):
    pm = ident.load_project_map(PROJECT_MAP)
    if args.all:
        items = _collect(pm)
        agg = defaultdict(lambda: [0, 0])
        all_ids = set()
        for it in items:
            if it.logical_id in (ident.GLOBAL_ID, ident.UNASSIGNED):
                continue
            all_ids.add(it.logical_id)
            agg[it.logical_id][0 if it.side == "claude" else 1] += 1
        dual_ids = sorted(p for p, v in agg.items() if v[0] > 0 and v[1] > 0)
        print(f"兩側都有的專案（{len(dual_ids)}）：{dual_ids or '（無）'}")

        # 磁碟目錄名＝id 經 norm.id_dir 安全轉換後的結果，比對前先把現況 id 也轉換
        existing_canonical = {d.name for d in CANONICAL.iterdir()
                              if d.is_dir() and not d.name.startswith("__")} if CANONICAL.exists() else set()
        dir_to_id = {norm.id_dir(i): i for i in all_ids}
        orphans = existing_canonical - set(dir_to_id)
        for oid in sorted(orphans):
            shutil.rmtree(CANONICAL / oid, ignore_errors=True)
            print(f"  🗑 專案身份已消失（可能路徑改名/搬移），整批清除 canonical/{oid}/")

        # 需要 collect（清殭屍 entry）：現存 canonical 且仍有任一側現況 ∪ 現在才變雙側的新專案
        collect_ids = sorted({dir_to_id[d] for d in (existing_canonical & set(dir_to_id))} | set(dual_ids))
        projects = collect_ids
        # 已知殘留限制：若專案從雙側掉回單側，其 AGENTS.md/memory 顆粒不會在此清空，
        # 只有 canonical 內容會被 collect 清乾淨；完整清空受管 block 需之後再補。
    elif args.project:
        projects = [args.project]
        dual_ids = None
    else:
        print("用法：./msync sync --project <id> 或 ./msync sync --all")
        return 1
    total = 0
    for proj in projects:
        state = _load_state()
        gen = state.get("generation", 0) + 1
        _, a, u, n, r = _collect_into_canonical(pm, proj, gen)
        state["generation"] = gen
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if r:
            print(f"  🗑 [{proj}] 清除 {r} 顆殭屍 canonical entry")
        if dual_ids is not None and proj not in dual_ids:
            continue  # 只有仍是雙側的專案才進 distribute（已知殘留限制，見上）
        _, changed = _ensure_integrated(pm, proj, gen)
        if changed:
            print(f"  ⟳ [{proj}] 整合版已重新生成（LLM）")
        ops, *_ = _build_ops(pm, proj)
        total += _apply_ops(ops, args.yes, f"[{proj}]")
    if args.all and not getattr(args, "skip_skills", False):
        print("\n" + "─" * 50)
        total += _run_skills(pm, args.yes)
    if args.all and not getattr(args, "skip_mcp", False):
        print("\n" + "─" * 50)
        try:
            total += _run_mcp(pm, args.yes)
        except RuntimeError as e:
            print(f"⚠ 跳過 MCP 層：{e}")
    if args.yes:
        print(f"\n✓ sync 完成，共處理 {total} 檔。")
    return 0


_SKILL_TAG = {"mirror": "鏡像", "delete": "刪除殭屍技能", "canonical": "中央倉",
              "canonical_delete": "中央倉清除", "remove_marker": "升級為來源(移除標記)"}


def _run_skills(pm, yes):
    """技能層：全域＋專案級 SKILL.md 資料夾雙向鏡像。回實寫檔數。"""
    state = _load_state()
    gen = state.get("generation", 0) + 1
    extra = {it.marker_root for it in _collect(pm) if it.marker_root}
    pairs = skillsync.sync_pairs(pm, extra)
    all_ops, all_warns = [], []
    ts = datetime.now().isoformat(timespec="seconds")
    print("# skills（SKILL.md 整包雙向鏡像 · generation 新疊舊 · .memsync-origin 防回授）")
    for scope, c_root, x_root in pairs:
        c_sk, x_sk = skillsync.scan_root(c_root), skillsync.scan_root(x_root)
        if not c_sk and not x_sk and not (CANONICAL / "__skills__" / scope).exists():
            continue
        decisions, conflicts = skillsync.merge_pair(CANONICAL, scope, c_sk, x_sk, gen)
        ops, warns = skillsync.build_ops(CANONICAL, scope, c_root, x_root, decisions, c_sk, x_sk, gen, ts)
        label = "全域" if scope == skillsync.GLOBAL_SCOPE else scope
        n_un = sum(1 for d in decisions.values() if d["action"] == "unchanged")
        print(f"\n## [{label}] claude {len(c_sk)} 顆 ↔ codex {len(x_sk)} 顆（unchanged {n_un}）")
        for w in conflicts:
            print(f"  {w}")
        for o in ops:
            print(f"  [{_SKILL_TAG[o['action']]}] {o['name']} → {_short(o['target'])}")
        for w in warns:
            print(w)
        if not ops and not conflicts:
            print("  ✓ 兩側一致，無動作")
        all_ops += ops
        all_warns += warns
    if not all_ops:
        print("\nskills：沒有要寫的變更。")
        return 0
    if not yes:
        print(f"\n（dry-run；共 {len(all_ops)} 個動作，加 --yes 才實寫）")
        return 0
    n = skillsync.apply_ops(all_ops)
    state["generation"] = gen
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ skills 已處理 {n} 個動作（generation {gen}）。")
    return n


def cmd_skills(args):
    pm = ident.load_project_map(PROJECT_MAP)
    _run_skills(pm, args.yes)
    return 0


def _mcp_classify(side, name, h, managed):
    m = (managed.get(side) or {}).get(name)
    return "mirror" if m == h else "source"


def _run_mcp(pm, yes):
    """MCP 層：機密外抽 → 中立描述 → 缺側補齊（受管鏡像）。回實寫檔數。"""
    envp = mcpsync.env_file_path(pm)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    state = _load_state()
    gen = state.get("generation", 0) + 1
    managed = state.get("mcp_managed", {"claude": {}, "codex": {}})
    c_servers, c_cfg_all = mcpsync.load_claude_servers()
    x_human, x_managed_toml, x_text = mcpsync.load_codex_servers()
    print("# mcp（機密外抽 → JSON ⇄ 中立描述 ⇄ TOML · 受管互寫）")
    print(f"共用機密檔：{_short(envp)}")
    writes = 0

    # ── 階段一：機密外抽（含撞名硬閘）──
    c_found = mcpsync.scan_secrets_claude(c_servers)
    x_found = mcpsync.scan_secrets_codex(x_human)
    env_vals = mcpsync.load_env_file(envp)
    new_vars, collisions = {}, []
    for name, loc, k, v, _newv in c_found:
        var = k if loc == "env" else mcpsync._varname(f"{name}_{k}")
        if env_vals.get(var, v) != v or new_vars.get(var, v) != v:
            collisions.append((name, var))
        else:
            new_vars[var] = v
    for name, k, v in x_found:
        var = mcpsync._varname(name + "_BEARER") if k == "__bearer__" else k
        if env_vals.get(var, v) != v or new_vars.get(var, v) != v:
            collisions.append((name, var))
        else:
            new_vars[var] = v
    skip_servers = {c[0] for c in collisions}
    for name, var in collisions:
        print(f"⛔ 撞名硬閘：server `{name}` 的變數 {var} 與既有值不同 → 該 server 整顆跳過（不同步、不改寫）")
    c_found = [f for f in c_found if f[0] not in skip_servers]
    x_found = [f for f in x_found if f[0] not in skip_servers]

    c_new_text = x_new_text = None
    if c_found and c_cfg_all is not None:
        c_new_text = mcpsync.rewrite_claude_secrets(json.loads(json.dumps(c_cfg_all)), c_found)
        err = mcpsync.validate_claude(c_new_text, c_found)
        if err:
            print(f"⛔ Claude 設定改寫驗證失敗：{err}（不落地）")
            c_new_text = None
    if x_found and x_text:
        x_new_text = mcpsync.rewrite_codex_secrets(x_text, x_found)
        err = mcpsync.validate_codex(x_new_text, x_found)
        if err:
            print(f"⛔ Codex 設定改寫驗證失敗：{err}（不落地）")
            x_new_text = None
    if c_found or x_found:
        print(f"\n## 機密外抽（{len(new_vars)} 個變數 → {_short(envp)}）")
        for name, loc, k, v, newv in c_found:
            print(f"  [claude:{name}] {loc}.{k} = {mcpsync.mask(v)} → {newv}")
        for name, k, v in x_found:
            var = mcpsync._varname(name + "_BEARER") if k == "__bearer__" else k
            print(f"  [codex:{name}] env.{k if k != '__bearer__' else 'bearer_token'} = {mcpsync.mask(v)} → env_vars[{var}]")
        print(f"  shell profile 請加一行：[ -f \"{envp}\" ] && source \"{envp}\"")
        if yes:
            merged = dict(env_vals)
            merged.update(new_vars)
            mcpsync.write_env_file(envp, merged)
            writes += 1
            if c_new_text is not None:
                mcpsync.backup(mcpsync.claude_config_path(), ts)
                mcpsync.claude_config_path().write_text(c_new_text, encoding="utf-8")
                writes += 1
            if x_new_text is not None:
                mcpsync.backup(mcpsync.codex_config_path(), ts)
                mcpsync.codex_config_path().write_text(x_new_text, encoding="utf-8")
                writes += 1
            print(f"  ✓ 已外抽並改寫（原檔備份 .memsync-bak-{ts}）")

    # ── 用外抽後狀態（實寫後重讀；dry-run 用記憶體模擬）算描述 ──
    if yes and (c_new_text or x_new_text):
        c_servers, c_cfg_all = mcpsync.load_claude_servers()
        x_human, x_managed_toml, x_text = mcpsync.load_codex_servers()
    else:
        if c_new_text:
            c_servers = dict(json.loads(c_new_text).get("mcpServers") or {})
        if x_new_text:
            span = mcpsync._managed_span(x_new_text)
            ht = (x_new_text[:span[0]] + x_new_text[span[1]:]) if span else x_new_text
            x_human = dict(mcpsync.tomllib.loads(ht).get("mcp_servers") or {})

    descs = {}
    for side, servers, fn in (("claude", c_servers, mcpsync.descriptor_from_claude),
                              ("codex", {**x_human, **x_managed_toml}, mcpsync.descriptor_from_codex)):
        for name, cfg in servers.items():
            if name in skip_servers:
                continue
            d, why = fn(name, cfg)
            if d is None:
                print(f"⛔ [{side}:{name}] 無法翻譯：{why}（跳過）")
                continue
            descs.setdefault(name, {})[side] = d

    # ── 裁決：每顆 server 選出勝方描述（人工 vs 人工不一致 → 只警示不覆蓋）──
    mdir = CANONICAL / "__mcp__"
    mdir.mkdir(parents=True, exist_ok=True)
    known = {f.stem for f in mdir.glob("*.json")} | set(descs)
    wanted_codex, wanted_claude = {}, {}   # name → desc：該側應持有的受管鏡像（全量重生的基準）
    canonical_writes = []                  # (path, meta or None＝刪)
    for name in sorted(known):
        sides = descs.get(name, {})
        cf = mdir / f"{name}.json"
        can = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else None
        cls = {s: _mcp_classify(s, name, mcpsync.desc_hash(d), managed) for s, d in sides.items()}
        sources = [(s, d) for s, d in sides.items() if cls[s] == "source"]
        if not sources:
            if can or sides:
                print(f"  🗑 `{name}` 來源已刪 → 移除鏡像與中央倉")
                canonical_writes.append((cf, None))
            continue
        if len(sources) == 2 and mcpsync.desc_hash(sources[0][1]) != mcpsync.desc_hash(sources[1][1]):
            print(f"  ⚠ `{name}` 兩側人工設定不一致——memsync 只補缺、不改人工 server，請手動對齊")
            continue
        win_side, win = sources[0]
        h = mcpsync.desc_hash(win)
        if not can or can.get("content_sha256") != h:
            canonical_writes.append((cf, {**win, "origin_side": win_side, "content_sha256": h,
                                          "first_seen_generation": (can or {}).get("first_seen_generation", gen),
                                          "last_seen_generation": gen}))
        other = "codex" if win_side == "claude" else "claude"
        if cls.get(other) == "source":
            print(f"  ✓ `{name}` 兩側皆為人工且一致")
            continue
        (wanted_codex if other == "codex" else wanted_claude)[name] = win
        if other not in sides:
            print(f"  [補缺] `{name}`（{win_side} → {other}）")
        elif mcpsync.desc_hash(sides[other]) != h:
            print(f"  [更新鏡像] `{name}`（{win_side} → {other}）")
        else:
            print(f"  ✓ `{name}` 鏡像已同步")

    # ── 寫回：codex 受管註解區全量重生；claude 受管 key 差異化增刪 ──
    region = mcpsync.render_managed_region(list(wanted_codex.values()))
    new_x = mcpsync.upsert_managed_region(x_text, region)
    claude_adds, claude_dels = {}, []
    for name, d in wanted_claude.items():
        entry = mcpsync.claude_entry_from_desc(d)
        if c_servers.get(name) != entry:
            claude_adds[name] = entry
    for name, old_h in (managed.get("claude") or {}).items():
        cur = descs.get(name, {}).get("claude")
        if name not in wanted_claude and cur is not None and mcpsync.desc_hash(cur) == old_h:
            claude_dels.append(name)   # 純鏡像且不再需要 → 刪；被使用者改過的已升級為來源，不會進來

    if new_x != x_text:
        print(f"  [寫回] Codex 受管區（{len(wanted_codex)} 顆 server）→ {_short(mcpsync.codex_config_path())}")
        if yes:
            if mcpsync.codex_config_path().exists():
                mcpsync.backup(mcpsync.codex_config_path(), ts)
            mcpsync.codex_config_path().parent.mkdir(parents=True, exist_ok=True)
            mcpsync.codex_config_path().write_text(new_x, encoding="utf-8")
            writes += 1
    if (claude_adds or claude_dels) and c_cfg_all is not None:
        print(f"  [寫回] Claude mcpServers（＋{len(claude_adds)} －{len(claude_dels)}）→ {_short(mcpsync.claude_config_path())}")
        if yes:
            cur = json.loads(mcpsync.claude_config_path().read_text(encoding="utf-8"))
            cur.setdefault("mcpServers", {})
            for k, v in claude_adds.items():
                cur["mcpServers"][k] = v
            for k in claude_dels:
                cur["mcpServers"].pop(k, None)
            mcpsync.backup(mcpsync.claude_config_path(), ts)
            mcpsync.claude_config_path().write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            writes += 1
    if yes:
        for cf, meta in canonical_writes:
            if meta is None:
                cf.unlink(missing_ok=True)
            else:
                cf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        managed = {"claude": {n: mcpsync.desc_hash(d) for n, d in wanted_claude.items()},
                   "codex": {n: mcpsync.desc_hash(d) for n, d in wanted_codex.items()}}

    if yes:
        state["mcp_managed"] = managed
        state["generation"] = gen
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ mcp 已處理 {writes} 檔（generation {gen}）。")
    else:
        print("\n（dry-run；加 --yes 才實寫）")
    return writes


def cmd_mcp(args):
    pm = ident.load_project_map(PROJECT_MAP)
    try:
        _run_mcp(pm, args.yes)
    except RuntimeError as e:
        print(f"⚠ {e}")
        return 1
    return 0


def _detect_drive():
    """自動偵測 Google Drive 資料夾（含中文/英文掛載名與 CloudStorage 實際掛載點）。"""
    home = Path.home()
    cands = [home / "我的雲端硬碟", home / "Google Drive"]
    cs = home / "Library" / "CloudStorage"
    if cs.exists():
        for g in sorted(cs.glob("GoogleDrive-*")):
            cands += [g / "我的雲端硬碟", g / "My Drive"]
    for c in cands:
        if c.is_dir():
            return c
    return None


def cmd_setup(args):
    """自我引導（冪等，可重複執行）：project_map／機密檔位置／shell profile 載入行，一次備齊。"""
    print("# setup（自我引導，冪等）")
    if not PROJECT_MAP.exists():
        example = HUB / "project_map.example.json"
        if example.exists():
            shutil.copy(example, PROJECT_MAP)
            print("✓ 已從範本建立 project_map.json")
        else:
            # 打包執行檔模式的資料目錄沒有範本檔，改由 cmd_map 生成骨架
            cmd_map(args)
    pm = json.loads(PROJECT_MAP.read_text(encoding="utf-8"))
    cur = (pm.get("mcp_env_file") or "").strip()
    if not cur:
        drive = _detect_drive()
        envp = (drive / "memsync" / "mcp.env") if drive else Path.home() / ".config" / "memsync" / "mcp.env"
        pm["mcp_env_file"] = str(envp)
        PROJECT_MAP.write_text(json.dumps(pm, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ 機密檔位置：{_short(envp)}" + ("（偵測到雲端硬碟，隨 Google 備份；資料夾勿設共用）" if drive else "（本機預設）"))
    else:
        envp = Path(cur).expanduser()
        print(f"✓ 機密檔位置（沿用既有設定）：{_short(envp)}")
    envp.parent.mkdir(parents=True, exist_ok=True)

    marker = "# memsync mcp env"
    line = f'[ -f "{envp}" ] && source "{envp}"  {marker}'
    profiles = [p for p in (Path.home() / ".zshrc", Path.home() / ".bashrc") if p.exists()] \
        or [Path.home() / ".zshrc"]
    for prof in profiles:
        text = prof.read_text(encoding="utf-8", errors="replace") if prof.exists() else ""
        kept = [l for l in text.split("\n") if marker not in l]
        new = "\n".join(kept).rstrip("\n") + ("\n" if any(s.strip() for s in kept) else "") + line + "\n"
        if new != text:
            prof.write_text(new, encoding="utf-8")
            print(f"✓ 機密檔載入行已寫進 {_short(prof)}（開新終端機生效）")
        else:
            print(f"✓ {_short(prof)} 載入行已存在")
    print("setup 完成。")
    return 0


def cmd_plugins(args):
    c_servers, _ = mcpsync.load_claude_servers()
    try:
        x_h, x_m, _ = mcpsync.load_codex_servers()
    except RuntimeError as e:
        print(f"⚠ {e}（plugins 報告改以 Claude 側為準，Codex 側 MCP 略過）")
        x_h, x_m = {}, {}
    print("\n".join(pluginsync.report(set(c_servers), set(x_h) | set(x_m))))
    return 0


def cmd_verify(args):
    """冪等自檢：重算 ops，確認既有受管內容與現況一致（無漂移）。"""
    project = args.project
    pm = ident.load_project_map(PROJECT_MAP)
    if not store.load_canonical_entries(CANONICAL, project):
        print(f"中央倉 canonical/{project}/ 是空的")
        return 1
    ops, *_ = _build_ops(pm, project)
    drift = [o for o in ops if o["action"] in ("create", "update", "delete")]
    blocked = [o for o in ops if o["action"] == "BLOCKED"]
    for o in ops:
        print(f"  {o['action']:9} [{_TAG[o['typ']]}] {_short(o['target'])}")
    ok = not drift and not blocked
    print(f"結論：{'✓ 冪等（無漂移）' if ok else '⚠ 有差異/擋閘'}")
    return 0 if ok else 1


def _todo(args):
    print(f"[{args._name}] 尚未實作（P3+）。目前開放：scan/map/status/plan/apply/verify。")
    return 0


def cmd_firstrun(args):
    """互動三步上手：①自我引導 setup ②scan 看對映 ③確認後 sync --all + rules。"""
    print("=== cc-codex-sync 初次設定 ===\n")
    if shutil.which("codex") is None and compat.codex_bin() == "codex":
        print("⚠ 找不到 codex CLI：LLM 整合/去重會停用（其餘功能照常）。裝好 codex 後再跑一次即可。\n")
    print(f"[1/3] 資料目錄：{_short(HUB)}")
    cmd_setup(args)  # 冪等自我引導：project_map／機密檔位置／shell 載入行
    print("\n[2/3] 掃描兩側記憶/規則、產出專案對映報告（唯讀）…\n")
    cmd_scan(args)
    print("\n[3/3] 以上是兩側對映現況。若『疑似同源』清單有你認得的同一專案，")
    print("      先去 project_map.json 補 override 再繼續；沒有就直接同步。")
    try:
        ans = input("\n現在執行第一次同步（sync --all + rules）？ [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans != "y":
        print("已略過。之後可隨時執行：sync --all --yes 與 rules --yes")
        return 0
    ns = argparse.Namespace(project=None, all=True, yes=True)
    cmd_sync(ns)
    cmd_rules(argparse.Namespace(yes=True))
    print("\n✓ 初次設定完成。日常只需重跑「同步」即可。")
    return 0


def _menu():
    """雙擊執行檔（無參數＋互動終端）時的選單。"""
    first_time = not PROJECT_MAP.exists()
    print("=== cc-codex-sync — Claude Code ⇄ Codex 記憶/規則同步 ===")
    print(f"    資料目錄：{_short(HUB)}\n")
    items = [
        ("1", "初次設定（自我引導 → 掃描 → 首次同步）", lambda: cmd_firstrun(argparse.Namespace())),
        ("2", "一鍵同步（記憶 sync --all + 規則 rules）",
         lambda: (cmd_sync(argparse.Namespace(project=None, all=True, yes=True)),
                  cmd_rules(argparse.Namespace(yes=True)))),
        ("3", "看現況（status）", lambda: cmd_status(argparse.Namespace())),
        ("4", "掃描對映報告（scan，唯讀）", lambda: cmd_scan(argparse.Namespace())),
        ("0", "離開", None),
    ]
    for k, label, _ in items:
        print(f"  {k}. {label}")
    default = "1" if first_time else "2"
    try:
        pick = input(f"\n選擇 [{default}]: ").strip() or default
    except EOFError:
        return 0
    for k, _, fn in items:
        if pick == k:
            if fn is None:
                return 0
            fn()
            break
    else:
        print("無此選項。")
    try:
        input("\n完成。按 Enter 關閉。")
    except EOFError:
        pass
    return 0


def main():
    compat.ensure_utf8_stdout()
    # 無參數＋互動終端（雙擊執行檔的情境）→ 進選單；有參數照舊走 CLI
    if len(sys.argv) == 1 and sys.stdin.isatty():
        return _menu()
    parser = argparse.ArgumentParser(prog="memsync", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("firstrun", help="互動三步上手：自我引導→掃描→首次同步").set_defaults(func=cmd_firstrun)
    sub.add_parser("scan", help="跨兩側採集+對映報告（唯讀）").set_defaults(func=cmd_scan)
    sub.add_parser("map", help="產/列 project_map.json 人工對照").set_defaults(func=cmd_map)
    sub.add_parser("status", help="印 hub 現況").set_defaults(func=cmd_status)

    sp_collect = sub.add_parser("collect", help="兩側來源 → 寫進中央倉 canonical/（generation 疊代）")
    sp_collect.add_argument("--project", required=True)
    sp_collect.set_defaults(func=cmd_collect)

    sp_plan = sub.add_parser("plan", help="dry-run：中央倉→兩側 受管 block diff（不寫）")
    sp_plan.add_argument("--project", required=True)
    sp_plan.set_defaults(func=cmd_plan)

    sp_apply = sub.add_parser("apply", help="記憶層寫回受管 block（需 --yes）")
    sp_apply.add_argument("--project", required=True)
    sp_apply.add_argument("--yes", action="store_true")
    sp_apply.set_defaults(func=cmd_apply)

    sp_verify = sub.add_parser("verify", help="冪等自檢（重算 ops 確認無漂移）")
    sp_verify.add_argument("--project", required=True)
    sp_verify.set_defaults(func=cmd_verify)

    sp_rules = sub.add_parser("rules", help="規則層雙向互寫（CLAUDE.md ↔ AGENTS.md，剝除 memsync 區塊）")
    sp_rules.add_argument("--yes", action="store_true")
    sp_rules.set_defaults(func=cmd_rules)

    sp_sync = sub.add_parser("sync", help="一鍵 collect+apply（記憶層）；--all 跑所有兩側都有的專案＋自動串技能/MCP")
    sp_sync.add_argument("--project", default=None)
    sp_sync.add_argument("--all", action="store_true")
    sp_sync.add_argument("--yes", action="store_true")
    sp_sync.add_argument("--skip-skills", action="store_true", help="--all 時跳過技能層")
    sp_sync.add_argument("--skip-mcp", action="store_true", help="--all 時跳過 MCP 層")
    sp_sync.set_defaults(func=cmd_sync)

    sp_skills = sub.add_parser("skills", help="技能層雙向鏡像（全域＋專案級 SKILL.md 整包 · 防回授標記）")
    sp_skills.add_argument("--yes", action="store_true")
    sp_skills.set_defaults(func=cmd_skills)

    sp_mcp = sub.add_parser("mcp", help="MCP 層同步（機密外抽 → JSON⇄TOML 受管互寫）")
    sp_mcp.add_argument("--yes", action="store_true")
    sp_mcp.set_defaults(func=cmd_mcp)

    sub.add_parser("plugins", help="plugin 拆解盤點（唯讀：列出內含技能/MCP 與對側覆蓋）").set_defaults(func=cmd_plugins)
    sub.add_parser("setup", help="自我引導（冪等）：project_map／機密檔位置自動偵測／shell profile 載入行").set_defaults(func=cmd_setup)

    for name in ("diff", "rollback"):
        sp = sub.add_parser(name, help="P3+ 尚未實作")
        sp.set_defaults(func=_todo, _name=name)
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
