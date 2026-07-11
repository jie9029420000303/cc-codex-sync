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
./msync skills  [--yes]              # 技能層雙向鏡像（全域＋專案級 SKILL.md 整包）
./msync mcp     [--yes]              # MCP 層同步（機密外抽 → JSON⇄TOML 受管互寫）
./msync plugins                      # plugin 拆解盤點（唯讀，不搬殼）
./msync verify  --project <id>       # 冪等自檢
./msync status                       # hub 現況
# sync --all 會自動串技能/MCP 層（--skip-skills / --skip-mcp 可跳過）
# 尚未實作：diff / rollback
```

## 技能層（skills）— SKILL.md 整包雙向鏡像
兩側同讀 agentskills.io 開放標準，鏡像即同步、不做內容揉合：
- 同步對：全域 `~/.claude/skills/ ↔ ~/.codex/skills/`＋專案級 `<repo>/.claude/skills/ ↔ <repo>/.codex/skills/`
  （repo 來自 project_map `match_cwds` 與 scan 根；專案級鏡像寫進 git 工作樹，記得自己 commit）
- 防回授：鏡像資料夾內放 `.memsync-origin`（origin_side＋content_sha256）。標記在且 hash 未變＝我們的鏡像，
  不當來源收回；hash 變了＝使用者真的改過 → 升級為來源、新疊舊，勝出側舊標記移除
- 衝突：與 hub 基準不同者為新 → 整包新疊舊；首輪無基準以資料夾 mtime 較新者勝（印衝突警示）
- 殭屍：來源側刪除 → 對側鏡像與 canonical 一併立即刪除
- 技能檔含硬編路徑（`~/.claude/`、`/Users/...`）→ 鏡像時警示

## MCP 層（mcp）— 機密外抽＋受管互寫
1. **機密外抽（首輪一次性）**：兩側設定中的明文 token 抽到共用 env 檔（`project_map.json` 的
   `mcp_env_file`，預設 `~/.config/memsync/mcp.env`；chmod 600、勿入 git、資料夾勿共用），
   兩側改寫成引用——Claude 用原生 `${VAR}` 展開、Codex 用 `env_vars = ["VAR"]` 轉發。
   shell profile 加一行 `[ -f "<env檔>" ] && source "<env檔>"`。改寫前備份 `.memsync-bak-<ts>`、
   改寫後 JSON/TOML 重解析＋語意驗證，不過即 BLOCKED 不落地。
2. **同步**：JSON ⇄ 中立描述 ⇄ TOML。Codex 側投影住在 config.toml 尾端 `# MEMSYNC:BEGIN mcp` 受管
   註解區（全量重生）；Claude 側投影 key 記錄於 state.json `mcp_managed`。**人工 server 除外抽外一律
   不改**：兩側人工設定不一致只警示、不覆蓋（server 設定含工具特有旗標，蓋錯代價高於技能）。
3. **硬閘**：同名環境變數跨 server 不同值 → 該 server 整顆跳過；SSE／不可翻譯的 header 引用 → 跳過並說明。
   dry-run 輸出一律遮罩機密值。

## Plugin（plugins）— 只拆不搬殼
兩家 plugin 打包格式不通用。`plugins` 唯讀盤點兩側 plugin 內含的技能／MCP 與對側覆蓋狀況，
缺的導引去跑 `skills`／`mcp`；plugin 殼由各工具自行管理安裝。

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

## 給同事：第一次使用

**最快：下載執行檔（免裝 Python）**

macOS（Apple Silicon）——貼進「終端機」執行（用指令下載不會觸發 Gatekeeper 警告）：
```bash
cd ~/Downloads && curl -LO https://github.com/jie9029420000303/cc-codex-sync/releases/latest/download/cc-codex-sync-macos-arm64 && chmod +x cc-codex-sync-macos-arm64 && ./cc-codex-sync-macos-arm64
```
Windows——到 [Releases](../../releases) 下載 `cc-codex-sync-windows-x64.exe` 雙擊；若 SmartScreen 攔截：
「其他資訊」→「仍要執行」。（Intel Mac 請走下方原始碼模式）

執行後出互動選單，第一次選「初次設定」即完成三步上手；日常選「一鍵同步」。
資料目錄在 `~/.cc-codex-sync/`。

若 macOS 用瀏覽器下載出現「來自未識別的開發者」：這是未簽章執行檔的標準警告
（Apple 簽章需付費開發者帳號），解法擇一——終端機跑
`xattr -d com.apple.quarantine ~/Downloads/cc-codex-sync-macos-arm64`，
或 系統設定 → 隱私權與安全性 → 捲到底按「強制打開」。
（Windows 版由 CI 建置並通過煙霧測試，尚未實機完整回歸）

**或從原始碼跑**（需 Python 3.9+）。本 repo 只含**工具本體**；`canonical/`、`state.json`、
`runs/`、`project_map.json` 都是個人資料/衍生快取，已 `.gitignore`，clone 下來不會拿到別人的實際記憶內容。
```bash
# 最簡單：Finder 雙擊「一鍵同步.command」——它會自動跑 setup（建 project_map、
# 偵測 Google Drive 決定機密檔位置、寫 shell 載入行），再 sync --all＋rules。
# 或手動：
./msync firstrun                                # 互動三步上手（自我引導→掃描→確認後首次同步）
./msync setup                                   # 只跑自我引導（冪等，可重複跑）
./msync scan                                    # 唯讀，先看哪些專案兩邊都有
./msync map                                     # 看 suspected-pair 提示，填進 project_map.json
./msync sync --all --yes                        # 記憶＋技能＋MCP 層同步
./msync rules --yes                              # 規則層同步
```
前置需求：已安裝並登入 Claude Code 與 Codex CLI（LLM 整合/去重呼叫 `codex`；缺少時該功能自動停用、其餘照常）。
Python 3.9+ 可用；**MCP 層同步需 3.11+**（內建 tomllib），3.9/3.10 會明確提示並跳過該層（執行檔版內建 3.12 無此限制）。
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
- **P5 技能/MCP/plugin 三層** ✅ `skills`（全域+專案級整包鏡像・.memsync-origin 防回授・新疊舊）、
  `mcp`（機密外抽→受管互寫・改寫後重解析驗證・撞名硬閘）、`plugins`（唯讀拆解盤點）、
  `sync --all` 自動串（--skip-skills/--skip-mcp）

## 目錄
- `memsync/` — 程式：`_v3mapper.py`(vendored 對映核心,勿改) / `identity` / `readers` / `normalize` / `store`(中央倉+殭屍清除) / `semantic`(語意去重) / `blocks` / `rules` / `skillsync`(技能層) / `mcpsync`(MCP層+機密外抽) / `pluginsync`(plugin拆解) / `cli`
- `canonical/<id>/entries/`（gitignore） — 中央倉單一真值（一檔一事實），可由 collect 重新生成
- `runs/<ts>/`（gitignore） — scan 報告
- `superseded/` — 已停用的舊 cron 備份
- `project_map.json`（gitignore，個人設定） / `project_map.example.json`（共用範本）
