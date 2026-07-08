"""LLM 整合引擎：把同專案兩側的相關記憶/規則揉成「一份」。

不是摘要壓縮。硬性要求：數字/路徑/指令/檔名/程式碼原文保留，不得改寫或省略；
LLM 只在邏輯層做去重、對齊矛盾、更新過時敘述、迭代加入新資訊。
迭代：以上一輪整合結果為基底，疊加本輪新的/變動的來源，不打掉重寫既有正確內容。
呼叫失敗/逾時回 None，由上層決定保守處置（不要拿半成品覆蓋既有整合版）。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

import compat

TIMEOUT_SECONDS = 600


def _mtime_label(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "未知"
    except Exception:
        return "未知"

_RULES = """硬性規則（違反視為失敗）：
1. 所有具體細節一律原文保留、不得改寫或省略：數字、檔案路徑、指令列、檔名、URL、程式碼片段、專有名詞、設定值。
2. 這是「整合」不是「摘要」——不要為了精簡而刪細節。寧可長，不可漏。
3. 去重：同一件事被多則重複描述時合併成一則，但把各則獨有的細節全部併入，不得只留其中一版。
4. 對齊矛盾：若有互相矛盾的敘述，以「來源檔較新者」為準（每則筆記標了來源檔最後修改時間），並在該處用「（更新：舊說法…已被…取代）」標明，不得靜默丟棄。
5. 迭代：若下方有「現有整合版」，以它為基底疊加本輪新增/變動內容，不要打掉重寫既有正確內容。
6. 用主題分節（## 標題）。輸出純 Markdown 正文，不要任何前言、結語、或「我整合了…」這類自我說明。
"""


def _run(prompt: str):
    outpath = None
    try:
        fd, outpath = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        compat.run_codex(
            ["exec", "--skip-git-repo-check", "-s", "read-only",
             "-c", "model_reasoning_effort=medium", "--color", "never", "-o", outpath],
            prompt, TIMEOUT_SECONDS,
        )
        text = Path(outpath).read_text(encoding="utf-8", errors="replace").strip()
        return text or None
    except Exception:
        return None
    finally:
        if outpath and os.path.exists(outpath):
            os.unlink(outpath)


def integrate_memory(entries, previous_integrated=None):
    """entries: 同一專案的原始記憶（兩側）。previous_integrated: 上一輪整合結果（迭代基底）或 None。"""
    if not entries:
        return None
    parts = [
        "你是記憶整合器。把下面來自兩個 AI 工具（Claude Code、Codex）關於同一個專案的筆記，整合成「一份」。",
        "",
        _RULES,
    ]
    if previous_integrated and previous_integrated.strip():
        parts += ["", "=== 現有整合版（以此為基底迭代）===", previous_integrated.strip()]
    parts += ["", "=== 本輪來源筆記 ==="]
    for i, e in enumerate(entries, 1):
        mt = _mtime_label(getattr(e, "source_mtime", 0.0))
        parts += ["", f"--- 筆記 {i}（來源工具：{e.origin_side}，來源檔最後修改：{mt}）---", e.body]
    return _run("\n".join(parts))


def integrate_rules(claude_rule, codex_rule, previous_integrated=None, claude_mtime=0.0, codex_mtime=0.0):
    """兩側人工規則整合成一份、自動對齊矛盾（使用者選擇規則也整合）。任一側缺就回另一側原文。
    衝突時以「來源檔較新者」為準（比 claude_mtime vs codex_mtime）。"""
    have = [(s, r, mt) for s, r, mt in (("Claude Code", claude_rule, claude_mtime),
                                        ("Codex", codex_rule, codex_mtime)) if r and r.strip()]
    if not have:
        return None
    if len(have) == 1:
        return have[0][1].strip()
    parts = [
        "你是規則整合器。把下面來自兩個 AI 工具的『行為規則』整合成「一份」讓兩邊共用。",
        "",
        _RULES,
        "額外：規則若有行為衝突（例如一邊『禁止 X』一邊『要用 X』），一律以「來源檔最後修改時間較新者」為準，"
        "在該條標明是採用哪一邊、捨棄哪一邊；產出一套彼此不矛盾的規則。保留所有具體指令格式、指令列、設定值原文。",
    ]
    if previous_integrated and previous_integrated.strip():
        parts += ["", "=== 現有整合版（以此為基底迭代）===", previous_integrated.strip()]
    for side, rule, mt in have:
        parts += ["", f"=== {side} 的規則（來源檔最後修改：{_mtime_label(mt)}）===", rule.strip()]
    return _run("\n".join(parts))
