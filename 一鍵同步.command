#!/usr/bin/env bash
# memsync 一鍵同步（在 Finder 雙擊即可執行）
# 記憶層 sync --all + 規則層 rules，兩側寫回同一份整合版受管 block。冪等：沒變更就不動。

HUB="$(cd "$(dirname "$0")" && pwd)"
CLI="$HUB/memsync/cli.py"

cd "$HUB" || { echo "找不到工具資料夾：$HUB"; exit 1; }

echo "==================================================="
echo "  memsync 一鍵同步  ·  $(date '+%Y-%m-%d %H:%M')"
echo "==================================================="
echo
echo ">>> 記憶層：sync --all（所有兩側都有的專案）"
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
