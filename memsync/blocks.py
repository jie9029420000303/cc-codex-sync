"""受管 block 渲染 + 完整性硬閘 + 全量重生 upsert。

block 用 BEGIN/END HTML 註解界定。寫回只動 BEGIN↔END 之間，block 外人工內容一字不動。
冪等：block 內容是 entry 集合的純函數（含 entry_hash）；entry_hash 不變就跳過寫入（連 ts 都不動）→ 連跑兩次 byte-identical。
"""
from __future__ import annotations

import hashlib
import json
import re

from normalize import slug as _slug


def entry_hash(entries) -> str:
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: (x.name, x.origin_path)):
        for part in (e.name, e.etype, e.content_sha256):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()[:12]


def _begin(kind, pid, h):
    return f"<!-- MEMSYNC:BEGIN kind={kind} project={pid} origin=memsync hash={h} do-not-edit -->"


def _end(kind, pid):
    return f"<!-- MEMSYNC:END kind={kind} project={pid} -->"


def render_block(entries, kind, pid, source_label, generated_ts) -> str:
    h = entry_hash(entries)
    out = [_begin(kind, pid, h),
           f"<!-- 由 memsync 自 {source_label} 同步生成・全量重生・手改會被覆蓋・generated {generated_ts} -->",
           "",
           f"## 共享記憶（來自 {source_label}）",
           ""]
    for e in sorted(entries, key=lambda x: (x.name, x.origin_path)):
        out.append(f"### {e.name}" + (f"（{e.etype}）" if e.etype else ""))
        if e.description:
            out.append(e.description)
        out.append("")
        if e.body:
            out.append(e.body)
            out.append("")
        out.append(f"<sub>origin: {e.origin_side} `{e.origin_path}` · sha {e.content_sha256[:8]}</sub>")
        out.append("")
    out.append(_end(kind, pid))
    return "\n".join(out)


def _span_re(kind, pid):
    return re.compile(
        r"<!-- MEMSYNC:BEGIN kind=%s project=%s [^>]*-->.*?<!-- MEMSYNC:END kind=%s project=%s -->"
        % (re.escape(kind), re.escape(pid), re.escape(kind), re.escape(pid)),
        re.DOTALL,
    )


def existing_hash(text, kind, pid):
    m = re.search(
        r"MEMSYNC:BEGIN kind=%s project=%s origin=memsync hash=([0-9a-f]+)"
        % (re.escape(kind), re.escape(pid)),
        text,
    )
    return m.group(1) if m else None


def integrity_check(text, kind, pid):
    """回 (ok, reason)。BEGIN/END 必須成對且唯一，否則拒寫該檔（硬閘）。"""
    begins = len(re.findall(r"MEMSYNC:BEGIN kind=%s project=%s" % (re.escape(kind), re.escape(pid)), text))
    ends = len(re.findall(r"MEMSYNC:END kind=%s project=%s" % (re.escape(kind), re.escape(pid)), text))
    if begins != ends:
        return False, f"BEGIN/END 不成對（{begins} begin / {ends} end）"
    if begins > 1:
        return False, f"受管 block 重複（{begins} 個）"
    return True, ""


def upsert(text, block, kind, pid):
    """把 block 整段替換進既有 block 位置；無則安全 append。block 外不動。"""
    rx = _span_re(kind, pid)
    spans = list(rx.finditer(text))
    if len(spans) == 1:
        s = spans[0]
        return text[:s.start()] + block + text[s.end():]
    if not text.strip():
        return block + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


_TYPE_MAP = {"memory": "project", "feedback": "feedback", "user": "user", "project": "project", "reference": "reference"}


def render_granule(entry) -> str:
    """把一顆 Codex 來源 entry 渲染成 Claude 原生 memory 顆粒（整檔由 memsync 擁有，無人工內容）。

    不放 generated ts，內容是 entry 的純函數 → 同內容 byte-identical（冪等）。
    frontmatter 值用 json.dumps 引號化，避免冒號/換行破壞 YAML。
    """
    etype = _TYPE_MAP.get(entry.etype, "project")
    desc = (entry.description or entry.name).replace("\n", " ").strip()
    out = [
        "---",
        f"name: {json.dumps('memsync-' + _slug(entry.name), ensure_ascii=False)}",
        f"description: {json.dumps(desc, ensure_ascii=False)}",
        "metadata:",
        f"  type: {etype}",
        "  origin: memsync",
        f"  origin_side: {entry.origin_side}",
        f"  content_sha256: {entry.content_sha256}",
        "---",
        "",
        entry.body,
        "",
    ]
    return "\n".join(out)


def render_index_block(entries, pid, generated_ts) -> str:
    """MEMORY.md 內的受管索引 block：只列 memsync 顆粒，不碰人工策展行。"""
    h = entry_hash(entries)
    out = [_begin("memory-index", pid, h),
           f"<!-- 由 memsync 同步自 Codex・全量重生・勿手改・generated {generated_ts} -->"]
    for e in sorted(entries, key=lambda x: (x.name, x.origin_path)):
        desc = (e.description or "").replace("\n", " ").strip()
        out.append(f"- [{e.name}](memsync-{_slug(e.name)}.md) — {desc}（來自 {e.origin_side}）")
    out.append(_end("memory-index", pid))
    return "\n".join(out)


def render_rule_block(body, pid, source_label, generated_ts) -> str:
    """規則層受管 block：把對方人工撰寫的規則放進一個 delimited 區塊。"""
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return "\n".join([
        _begin("rules", pid, h),
        f"<!-- 由 memsync 同步自 {source_label}・全量重生・勿手改・generated {generated_ts} -->",
        "",
        f"## 同步規則（來自 {source_label}）",
        "",
        body,
        "",
        _end("rules", pid),
    ])


def block_text(text, kind, pid):
    """取出既有受管 block 的整段文字（含標記），無則空字串。"""
    m = _span_re(kind, pid).search(text)
    return m.group(0) if m else ""


def growth_warn(old_text, new_text, kind, pid):
    """第二階回授兜底：受管 block 體積 >1.5x 成長時回警示字串（否則空）。"""
    ob, nb = block_text(old_text, kind, pid), block_text(new_text, kind, pid)
    if ob and nb and len(nb) > 1.5 * len(ob):
        return f"⚠ block 體積 {len(ob)}→{len(nb)} bytes（>1.5x，留意第二階換皮回授）"
    return ""


def render_doc_block(text, kind, pid, source_label, generated_ts) -> str:
    """把一份整合好的 markdown（整段）包成受管 block。hash 以整段文字算，idempotent。"""
    h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    return "\n".join([
        _begin(kind, pid, h),
        f"<!-- 由 memsync {source_label}・整合版全量重生・勿手改・generated {generated_ts} -->",
        "",
        (text or "").strip(),
        "",
        _end(kind, pid),
    ])


def render_integrated_granule(text, pid) -> str:
    """一個專案一顆整合版 Claude 原生 memory 顆粒（整檔 memsync 擁有，無 ts，內容純函數→冪等）。"""
    h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    return "\n".join([
        "---",
        f"name: {json.dumps('memsync-' + _slug(pid) + '-integrated', ensure_ascii=False)}",
        f"description: {json.dumps('memsync 整合版：' + pid + ' 跨工具共享知識', ensure_ascii=False)}",
        "metadata:",
        "  type: project",
        "  origin: memsync",
        f"  content_sha256: {h}",
        "---",
        "",
        (text or "").strip(),
        "",
    ])


def render_integrated_index(pid, generated_ts) -> str:
    """MEMORY.md 內指向整合版顆粒的一行受管索引（內容固定＝冪等）。"""
    h = hashlib.sha256(("memsync-integrated-index:" + pid).encode()).hexdigest()[:12]
    return "\n".join([
        _begin("memory-index", pid, h),
        f"<!-- 由 memsync 整合版索引・勿手改・generated {generated_ts} -->",
        f"- [memsync 整合版：{pid}](memsync-{_slug(pid)}-integrated.md) — 兩工具共享的整合知識",
        _end("memory-index", pid),
    ])
