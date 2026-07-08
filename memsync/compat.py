"""跨平台相容層（macOS / Windows / Linux）。

- codex 執行檔解析：Windows 上 npm 裝的是 codex.cmd shim，subprocess 清單形式
  不會自動解析 .cmd，必須用 shutil.which 找到完整路徑。
- subprocess 一律強制 UTF-8：Windows 預設 cp950 會把中文 prompt/輸出弄爛。
- 主控台輸出 UTF-8：Windows cmd.exe 預設編碼印中文會炸 UnicodeEncodeError。
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def codex_bin() -> str:
    for name in ("codex", "codex.cmd", "codex.exe"):
        found = shutil.which(name)
        if found:
            return found
    return "codex"  # 留給 subprocess 丟 FileNotFoundError，由呼叫端保守處置


def run_codex(args, input_text: str, timeout: int):
    return subprocess.run(
        [codex_bin(), *args],
        input=input_text, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
