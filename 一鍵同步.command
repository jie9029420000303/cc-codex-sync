#!/usr/bin/env bash
# memsync 一鍵同步（在 Finder 雙擊即可執行）
# setup 自我引導 → 記憶層 sync --all（自動串技能層＋MCP 層）→ 規則層 rules。
# 全程冪等：沒變更就不動；首次執行會自動偵測雲端硬碟、建 project_map、寫 shell 載入行。

HUB="$(cd "$(dirname "$0")" && pwd)"
CLI="$HUB/memsync/cli.py"

cd "$HUB" || { echo "找不到工具資料夾：$HUB"; exit 1; }

echo "==================================================="
echo "  memsync 一鍵同步  ·  $(date '+%Y-%m-%d %H:%M')"
echo "==================================================="
echo
echo ">>> 自我引導：setup（首次自動配置，之後冪等跳過）"
python3 "$CLI" setup
echo
echo ">>> 記憶層＋技能層＋MCP 層：sync --all"
python3 "$CLI" sync --all --yes
echo
echo ">>> 規則層：rules（CLAUDE.md ↔ AGENTS.md）"
python3 "$CLI" rules --yes
echo
echo ">>> 現況："
python3 "$CLI" status
echo
echo "==================================================="
echo "  完成。按 Enter 關閉視窗。"
echo "==================================================="
read -r
