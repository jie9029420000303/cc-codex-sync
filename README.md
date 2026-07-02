# memsync — Codex ⇄ Claude Code 記憶/規則 雙向同步器

把 Codex 與 Claude Code 兩個 AI CLI 各自累積的「任務/對話記憶」與「設定規則」，以**邏輯專案**為單位
抓回來、揉合、再分送回兩邊的**每專案受管 block**，讓同一專案在兩個工具裡看到一致的記憶與規則。
目標：盡量平衡兩邊的使用體驗。

## 架構：中央 hub（防回授）

```
Claude 來源（唯讀）          Codex 來源（唯讀）
 memory/*.md · CLAUDE.md      rollout_summaries/*.md · AGENTS.md
        └──────── collect → resolve（複用 v3 三層對映）→ normalize ────────┘
                              ↓
            memsync HUB（本 repo） canonical/<專案>/entries · state.json(hash) · project_map.json
                              ↓  distribute（全量重生受管 block · dry-run→apply）
 <專案>/CLAUDE.md 受管 block          <專案>/AGENTS.md 受管 block
 （Claude 原生讀）                    （Codex 原生讀）
```

**禁區（絕不讀/寫）**：`~/.codex/memories/` 的 `MEMORY.md`/`raw_memories.md`/`*.sqlite`/`extensions/`
（pipeline 領地，手寫會被 `memory_consolidate_global` 洗掉）；以及 Claude 人工策展的 `MEMORY.md` 索引。

## 衝突解法：generation 疊代（不靠時間戳）

Codex 的 `updated_at`/檔案 `mtime` 已實機證實被 pipeline 批次改寫、不可信。
故改用 hub 的**輪次**當權威：hub 裡上一輪已搓揉的＝舊；這一輪從某工具新抓到、與 hub 基準不同的＝新 → 新疊舊。
搓揉結果成為下一輪的舊基準。只有第一次執行需特別處理。

## 防回授三道閘
1. provenance 標記跳過：採集時凡落在受管 block 內／帶 hub 標記者一律不收。
2. content-hash 去重：每顆 entry 有 `content_sha256`，命中即不新增。
3. 受管 block 全量重生（非 append）：block 大小只由 canonical 集合決定，與輪數無關。

## 指令
```
./msync scan                         # 跨兩側採集+對映報告（唯讀）
./msync map                          # 產/列 project_map.json 人工對照 + suspected-pair
./msync collect --project <id>       # 兩側來源 → 中央倉 canonical/（generation 疊代）
./msync plan    --project <id>       # dry-run：中央倉→兩側受管 block diff（不寫）
./msync apply   --project <id> --yes # 記憶層寫回（互補投影：AGENTS.md 收對方記憶 / Claude memory 顆粒）
./msync sync    --all --yes          # 一鍵 collect+apply 所有兩側都有的專案
./msync rules   [--yes]              # 規則層雙向互寫（CLAUDE.md ↔ AGENTS.md）
./msync verify  --project <id>       # 冪等自檢
./msync status                       # hub 現況
# 尚未實作：diff / rollback
```

## project_map.json — 人工專案身份對照
兩邊沒有天然乾淨的 key（同一邏輯專案常在兩側是不同路徑、甚至不同 git repo）。
對映優先序：**override > git_remote > path**。把同源專案用一個 `id` 收斂：
```json
{ "projects": [
  { "id": "example-project", "title": "範例：官網專案",
    "match_remotes": ["https://github.com/your-org/example-repo"],
    "match_cwds": ["/Users/you/Documents/example-project"] }
]}
```

## 給同事：第一次使用（工具與個人資料分離）
這個 repo 只共用**工具本體**（`memsync/`、`msync`、`一鍵同步.command`）。`canonical/`、`state.json`、
`runs/`、`project_map.json` 都是個人資料/衍生快取，已 `.gitignore`，clone 下來不會拿到別人的實際記憶內容。
```bash
cp project_map.example.json project_map.json   # 每人自己一份，填自己的專案對照
./msync scan                                    # 唯讀，先看哪些專案兩邊都有
./msync map                                     # 看 suspected-pair 提示，填進 project_map.json
./msync sync --all --yes                        # 記憶層同步
./msync rules --yes                              # 規則層同步
```
發布版 repo 的 history 為單一乾淨 commit，不含任何個人記憶/規則內容。

## 殭屍記憶清除（立即、無寬限期）
來源記憶（Claude memory 檔／Codex rollout 檔）被刪除或搬移後，下次 `collect`/`sync` 會：
- 立即刪除 canonical 對應 entry（不留寬限期）
- 一併刪除已寫到 Claude 端的 `memsync-*.md` 顆粒檔、更新 `MEMORY.md` 索引 block
- `sync --all` 額外做**專案級孤兒清除**：canonical 底下的專案目錄若在目前掃描中完全找不到對應（例如
  資料夾整個改名/搬移、身份跟著變了），整批刪除該目錄並印出訊息
- 已知殘留限制：專案若從「兩側都有」掉回「只剩一側」（未整個消失、只是其中一個工具不再碰它），
  canonical 內容會被正確清乾淨，但另一側已寫入的 AGENTS.md／CLAUDE.md 受管 block 目前不會自動清空

## Codex 端記憶去重（語意相似度，真的呼叫模型判斷）
Codex 每次對話可能對同一主題產生新的 rollout 摘要（標題不同）。`collect` 在新增 Codex 來源候選前，
會呼叫 `codex exec` 問「這兩則是不是同一件事」，真的重複就地更新既有 entry（不新增），不是關鍵字湊合。
呼叫失敗/逾時一律保守判不重複（寧可不去重，不可誤合併不同記憶）。

## 狀態
- **P0 地基** ✅ git init、vendoring v3 mapper、刪舊 cron（備份於 `superseded/`）
- **P1 唯讀對映** ✅ readers + identity + `scan`/`map`
- **P2 寫回引擎** ✅ normalizer + 受管 block + 完整性硬閘 + plan/apply/verify
- **P3 雙向閉環** ✅ 中心化 collect→canonical→distribute、互補投影、Claude 原生 memory 顆粒、防火牆#1
- **P4 規則層 + 硬化** ✅ 規則層雙向互寫（`rules`）、`sync --all`、block 成長 >1.5x 警示、generation 只由 collect 推進
- **記憶層雙向**：✅ 已實證（真實專案試點，冪等、防回授）
- **規則層雙向**：✅ 全域 + per-project（剝除 memsync 區塊防回授）

## 目錄
- `memsync/` — 程式：`_v3mapper.py`(vendored 對映核心,勿改) / `identity` / `readers` / `normalize` / `store`(中央倉+殭屍清除) / `semantic`(語意去重) / `blocks` / `rules` / `cli`
- `canonical/<id>/entries/`（gitignore） — 中央倉單一真值（一檔一事實），可由 collect 重新生成
- `runs/<ts>/`（gitignore） — scan 報告
- `superseded/` — 已停用的舊 cron 備份
- `project_map.json`（gitignore，個人設定） / `project_map.example.json`（共用範本）
