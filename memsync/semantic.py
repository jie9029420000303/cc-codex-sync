"""語意相似度去重判斷——真的呼叫模型判斷「是不是同一件事」，不是關鍵字/相似度湊合。

判斷「這兩則記憶是不是同一主題、保留一則會不會漏資訊」是分類/判斷型工作，
依專案原則交給模型、不交給確定性程式碼硬猜。呼叫失敗/逾時一律保守回 False（寧可不去重，不可誤合併）。
"""
from __future__ import annotations

import json
import subprocess

CODEX_BIN = "codex"
TIMEOUT_SECONDS = 25


def is_duplicate_topic(text_a: str, text_b: str) -> bool:
    prompt = (
        "以下兩則記憶筆記，是否談論同一件事、且保留其中一則就不會遺失重要資訊？"
        "只回一行 JSON，格式 {\"duplicate\": true} 或 {\"duplicate\": false}，不要任何其他文字。\n\n"
        f"=== 筆記 A ===\n{text_a[:3000]}\n\n=== 筆記 B ===\n{text_b[:3000]}\n"
    )
    try:
        proc = subprocess.run(
            [CODEX_BIN, "exec", "--skip-git-repo-check", "-s", "read-only",
             "-c", "model_reasoning_effort=low", "--color", "never"],
            input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
        for line in reversed(proc.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                return bool(json.loads(line).get("duplicate", False))
    except Exception:
        pass
    return False
