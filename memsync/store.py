"""Canonical hub store — 中央單一真值。所有記憶先進這裡，兩側只是它的投影。

canonical/<logical_id>/entries/<entry_key>.md：一檔一事實，frontmatter 帶 provenance + generation。
這是 generation 疊代 merge 的基準：上一輪已在 store 的＝舊；本輪 collect 進來、content 不同的＝新 → 新疊舊。
distribute 一律從這裡讀，不從原始來源讀（杜絕點對點）。

殭屍記憶清除（立即、無寬限期）：merge_into_canonical 每次都是某個 logical_id 的完整權威快照——
本輪沒被任何來源提到的既有 entry，視為來源已刪除/搬移，當場刪除，不留寬限期。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from normalize import Entry, slug as _slug
import semantic


def entry_key(e) -> str:
    """以來源檔路徑為基準（穩定，不受 frontmatter name／標題內容編輯影響），非以內容衍生的 name。"""
    stem = _slug(Path(e.origin_path).stem)
    h = hashlib.sha1(e.origin_path.encode("utf-8")).hexdigest()[:10]
    return f"{e.origin_side}__{stem}-{h}"


def _fm_dump(d: dict) -> str:
    out = ["---"]
    for k, v in d.items():
        out.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    out.append("---")
    return "\n".join(out)


def _fm_parse(text: str):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text.strip()
    meta = {}
    for line in m.group(1).split("\n"):
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if kv:
            try:
                meta[kv.group(1)] = json.loads(kv.group(2))
            except Exception:
                meta[kv.group(1)] = kv.group(2)
    return meta, m.group(2).strip()


def _write_entry(path: Path, e, first_gen: int, last_gen: int, merged_paths=()) -> None:
    fm = {
        "logical_id": e.logical_id, "origin_side": e.origin_side,
        "origin_path": e.origin_path, "name": e.name, "etype": e.etype,
        "description": e.description, "content_sha256": e.content_sha256,
        "source_mtime": getattr(e, "source_mtime", 0.0),
        "first_seen_generation": first_gen, "last_seen_generation": last_gen,
    }
    mp = sorted(set(merged_paths) - {e.origin_path})
    if mp:
        # 語意去重合併過的舊來源路徑：讓下輪 collect 直接命中此 entry，不再重判（決定持久化）
        fm["merged_paths"] = mp
    path.write_text(_fm_dump(fm) + "\n" + e.body + "\n", encoding="utf-8")


def input_hash(entries) -> str:
    """整合輸入指紋：所有來源 entry 的 content_sha256 排序後 hash。輸入沒變＝不必重跑 LLM 整合。"""
    h = hashlib.sha256()
    for s in sorted(e.content_sha256 for e in entries):
        h.update(s.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def load_integrated(canonical_root: Path, logical_id: str):
    """回 (整合後文字 or None, 上次整合的 input_hash or None)。"""
    d = canonical_root / logical_id
    doc = d / "integrated.md"
    meta = d / "integrated.meta.json"
    text = doc.read_text(encoding="utf-8", errors="replace") if doc.exists() else None
    h = None
    if meta.exists():
        try:
            h = json.loads(meta.read_text(encoding="utf-8")).get("input_hash")
        except Exception:
            h = None
    return text, h


def save_integrated(canonical_root: Path, logical_id: str, text: str, in_hash: str, generation: int) -> None:
    d = canonical_root / logical_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "integrated.md").write_text(text.rstrip() + "\n", encoding="utf-8")
    (d / "integrated.meta.json").write_text(
        json.dumps({"input_hash": in_hash, "generation": generation}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _find_semantic_duplicate(d: Path, e, current_keys: set):
    """只對 codex 來源的新候選找同主題重複（Claude 側由使用者自己策展，成長不是診斷出的問題）。
    只跟『這輪還沒配對過』的既有同側 entry 比對；呼叫失敗一律回 None（保守，不誤合併）。"""
    if e.origin_side != "codex":
        return None
    for f in sorted(d.glob(f"{e.origin_side}__*.md")):
        if f.stem in current_keys:
            continue  # 這輪已經配過的 key，跳過
        try:
            _, body = _fm_parse(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if semantic.is_duplicate_topic(e.body, body):
            return f
    return None


def merge_into_canonical(canonical_root: Path, logical_id: str, entries, generation: int):
    """generation 疊代：同來源內容相同→不動；不同→新疊舊；新→先試語意去重(codex側)否則新增。
    來源以 origin_path（含歷次合併的 merged_paths 別名）對應 entry——語意合併的決定持久化，
    下輪直接命中、不重判不重呼叫 LLM。本輪沒出現的既有 entry 立即刪除（無寬限期）。
    回 (added, updated, unchanged, removed)。"""
    d = canonical_root / logical_id / "entries"
    d.mkdir(parents=True, exist_ok=True)
    added = updated = unchanged = 0
    current_keys: set = set()

    # origin_path（含 merged_paths 別名）→ 既有 entry 檔 的索引
    path_index: dict = {}
    file_meta: dict = {}
    for f in d.glob("*.md"):
        meta, _ = _fm_parse(f.read_text(encoding="utf-8", errors="replace"))
        file_meta[f] = meta
        for p in [meta.get("origin_path")] + list(meta.get("merged_paths") or []):
            if p:
                path_index[p] = f

    for e in entries:
        hit = path_index.get(e.origin_path)
        if hit is not None:
            meta = file_meta.get(hit, {})
            first = meta.get("first_seen_generation", generation)
            aliases = set(meta.get("merged_paths") or []) | {meta.get("origin_path"), e.origin_path}
            aliases.discard(None)
            if meta.get("content_sha256") == e.content_sha256:
                # 內容沒變仍屬 unchanged，但 frontmatter 過時（缺 source_mtime／別名表）就補寫遷移
                if (meta.get("source_mtime") != getattr(e, "source_mtime", 0.0)
                        or set(meta.get("merged_paths") or []) != (aliases - {e.origin_path})):
                    _write_entry(hit, e, first, meta.get("last_seen_generation", generation), aliases)
                unchanged += 1
            else:
                _write_entry(hit, e, first, generation, aliases)
                updated += 1
            current_keys.add(hit.stem)
            continue
        dup = _find_semantic_duplicate(d, e, current_keys)
        if dup is not None:
            meta = file_meta.get(dup) or _fm_parse(dup.read_text(encoding="utf-8", errors="replace"))[0]
            first = meta.get("first_seen_generation", generation)
            aliases = set(meta.get("merged_paths") or []) | {meta.get("origin_path"), e.origin_path}
            aliases.discard(None)
            _write_entry(dup, e, first, generation, aliases)
            updated += 1
            current_keys.add(dup.stem)
        else:
            path = d / f"{entry_key(e)}.md"
            _write_entry(path, e, generation, generation)
            added += 1
            current_keys.add(path.stem)

    removed = 0
    for f in list(d.glob("*.md")):
        if f.stem not in current_keys:
            f.unlink()
            removed += 1
    return added, updated, unchanged, removed


def rule_input_hash(*rule_texts) -> str:
    h = hashlib.sha256()
    for t in rule_texts:
        h.update((t or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _rule_file(canonical_root: Path, scope: str) -> Path:
    return canonical_root / "__rules__" / f"{_slug(scope)}.md"


def load_integrated_rule(canonical_root: Path, scope: str):
    f = _rule_file(canonical_root, scope)
    meta = f.with_suffix(".meta.json")
    text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else None
    h = None
    if meta.exists():
        try:
            h = json.loads(meta.read_text(encoding="utf-8")).get("input_hash")
        except Exception:
            h = None
    return text, h


def save_integrated_rule(canonical_root: Path, scope: str, text: str, in_hash: str) -> None:
    f = _rule_file(canonical_root, scope)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text.rstrip() + "\n", encoding="utf-8")
    f.with_suffix(".meta.json").write_text(
        json.dumps({"input_hash": in_hash, "scope": scope}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_canonical_entries(canonical_root: Path, logical_id: str):
    d = canonical_root / logical_id / "entries"
    out = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.md")):
        meta, body = _fm_parse(f.read_text(encoding="utf-8", errors="replace"))
        out.append(Entry(
            logical_id=meta.get("logical_id", logical_id),
            origin_side=meta.get("origin_side", "?"),
            origin_path=meta.get("origin_path", str(f)),
            name=meta.get("name", f.stem),
            etype=meta.get("etype", "memory"),
            description=meta.get("description", ""),
            body=body,
            content_sha256=meta.get("content_sha256", ""),
            source_mtime=meta.get("source_mtime", 0.0) or 0.0,
        ))
    return out
