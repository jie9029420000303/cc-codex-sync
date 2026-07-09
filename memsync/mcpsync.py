"""MCP 層同步：Claude(~/.claude.json JSON) ⇄ 中立描述 ⇄ Codex(~/.codex/config.toml TOML)。

機密外抽（首輪一次性搓揉，之後設定檔不含明文、可自由同步）：
  ① 明文機密抽到共用 env 檔（project_map 的 `mcp_env_file`，預設 ~/.config/memsync/mcp.env；
     chmod 600、永不進 hub/git、永不進同步範圍）
  ② 兩側設定改寫成引用——Claude 原生 `${VAR}` 展開；Codex 不支援展開，改用
     `env_vars = ["VAR"]` 環境轉發（HTTP 型用 `bearer_token_env_var`）
  ③ 環境變數名＝原始 env key（兩側工具都靠 shell profile source env 檔取得值）。
     同名不同值跨 server 撞名 → 該 server 硬閘跳過（寧可不同步，不可錯值）。

改寫使用者原檔的安全網：改寫前備份（.memsync-bak-<ts>）、改寫後重新解析驗證
（JSON/TOML parse ＋ 語意檢查：明文已消失、引用已就位），驗證不過 → BLOCKED 不落地。

受管邊界：Codex 側 memsync 投影的 server 全部住在 config.toml 尾端的
`# MEMSYNC:BEGIN mcp` 受管註解區（全量重生）；Claude 側投影的 server key 記錄在
state.json 的 mcp_managed，兩側人工自建的 server 除機密外抽外一律不碰。

dry-run 輸出一律遮罩機密值（只顯示變數名）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # 3.9/3.10：不讓整支 CLI 掛掉；MCP 層在入口明確拒絕（不可靜默把現有設定讀成空）
    tomllib = None

MANAGED_BEGIN = "# MEMSYNC:BEGIN mcp origin=memsync do-not-edit"
MANAGED_END = "# MEMSYNC:END mcp"

_SECRET_KEY_RE = re.compile(r"(key|token|secret|pass|password|credential|auth)", re.I)
_SECRET_VAL_RE = re.compile(r"^(sk-|ghp_|gho_|github_pat_|xox[bpars]-|AIza|ya29\.|Bearer\s+\S{16,})")
_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_VARNAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def claude_config_path() -> Path:
    return Path.home() / ".claude.json"


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def env_file_path(pm: dict) -> Path:
    return Path(pm.get("mcp_env_file") or "~/.config/memsync/mcp.env").expanduser()


def _varname(s: str) -> str:
    v = _VARNAME_RE.sub("_", s).strip("_").upper()
    return v or "VAR"


def is_secret(key: str, val) -> bool:
    if not isinstance(val, str) or not val or "${" in val:
        return False  # 已是變數引用（含 "Bearer ${VAR}" 這類複合值）＝已外抽，不再視為明文
    return bool(_SECRET_KEY_RE.search(key) or _SECRET_VAL_RE.match(val))


# ---------- 讀兩側 ----------

def load_claude_servers():
    """回 (servers dict, 整份 config dict or None)。"""
    p = claude_config_path()
    if not p.exists():
        return {}, None
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}, None
    return dict(cfg.get("mcpServers") or {}), cfg


def _managed_span(text: str):
    b, e = text.find(MANAGED_BEGIN), text.find(MANAGED_END)
    if b != -1 and e != -1 and e > b:
        return b, e + len(MANAGED_END)
    return None


def load_codex_servers():
    """回 (人工 servers, 受管 servers, 原始全文)。受管區內外分開解析。"""
    if tomllib is None:
        raise RuntimeError("MCP 層同步需要 Python 3.11+（內建 tomllib）；其餘各層不受影響")
    p = codex_config_path()
    if not p.exists():
        return {}, {}, ""
    text = p.read_text(encoding="utf-8", errors="replace")
    span = _managed_span(text)
    managed_text = text[span[0]:span[1]] if span else ""
    human_text = (text[:span[0]] + text[span[1]:]) if span else text

    def parse(t):
        try:
            return dict(tomllib.loads(t).get("mcp_servers") or {})
        except Exception:
            return {}
    return parse(human_text), parse(managed_text), text


# ---------- 中立描述 ----------

def descriptor_from_claude(name: str, cfg: dict):
    """回 (descriptor or None, skip_reason)。翻譯不了的標記 claude_only。"""
    typ = (cfg.get("type") or "stdio").lower()
    d = {"name": name, "transport": typ, "command": "", "args": [], "env_plain": {},
         "env_refs": [], "url": "", "bearer_var": "", "headers_plain": {}}
    if typ == "stdio":
        d["command"] = cfg.get("command") or ""
        d["args"] = list(cfg.get("args") or [])
        for k, v in (cfg.get("env") or {}).items():
            m = _REF_RE.match(v) if isinstance(v, str) else None
            if m:
                if m.group(1) != k:
                    return None, f"env {k} 引用了不同名變數 ${{{m.group(1)}}}（Codex 無法改名轉發）"
                d["env_refs"].append(k)
            elif is_secret(k, v):
                return None, f"env {k} 仍是明文機密（需先外抽）"
            else:
                d["env_plain"][k] = v
    elif typ in ("http", "sse"):
        if typ == "sse":
            return None, "SSE 傳輸 Codex 端不支援"
        d["url"] = cfg.get("url") or ""
        for k, v in (cfg.get("headers") or {}).items():
            mm = re.match(r"^Bearer\s+\$\{([A-Za-z_][A-Za-z0-9_]*)\}$", v or "")
            if k.lower() == "authorization" and mm:
                d["bearer_var"] = mm.group(1)
            elif isinstance(v, str) and "${" in v:
                return None, f"header {k} 含變數引用（Codex 端 header 不支援展開）"
            elif is_secret(k, v):
                return None, f"header {k} 仍是明文機密（需先外抽）"
            else:
                d["headers_plain"][k] = v
    else:
        return None, f"未知 type={typ}"
    return d, ""


def descriptor_from_codex(name: str, cfg: dict):
    d = {"name": name, "transport": "stdio", "command": "", "args": [], "env_plain": {},
         "env_refs": [], "url": "", "bearer_var": "", "headers_plain": {}}
    if cfg.get("url"):
        d["transport"] = "http"
        d["url"] = cfg["url"]
        d["bearer_var"] = cfg.get("bearer_token_env_var") or ""
    else:
        d["command"] = cfg.get("command") or ""
        d["args"] = list(cfg.get("args") or [])
        for ev in cfg.get("env_vars") or []:
            if isinstance(ev, str):
                d["env_refs"].append(ev)
        for k, v in (cfg.get("env") or {}).items():
            if is_secret(k, v):
                return None, f"env {k} 仍是明文機密（需先外抽）"
            d["env_plain"][k] = v
    d["env_refs"] = sorted(set(d["env_refs"]))
    return d, ""


def desc_hash(d: dict) -> str:
    core = {k: d[k] for k in ("transport", "command", "args", "env_plain", "env_refs",
                              "url", "bearer_var", "headers_plain")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------- 投影回兩側 ----------

def claude_entry_from_desc(d: dict) -> dict:
    if d["transport"] == "http":
        out = {"type": "http", "url": d["url"]}
        headers = dict(d["headers_plain"])
        if d["bearer_var"]:
            headers["Authorization"] = f"Bearer ${{{d['bearer_var']}}}"
        if headers:
            out["headers"] = headers
        return out
    out = {"type": "stdio", "command": d["command"]}
    if d["args"]:
        out["args"] = list(d["args"])
    env = {k: f"${{{k}}}" for k in d["env_refs"]}
    env.update(d["env_plain"])
    if env:
        out["env"] = env
    return out


def _toml_str(s) -> str:
    return json.dumps(str(s), ensure_ascii=False)  # JSON 字串轉義與 TOML basic string 相容


def codex_table_from_desc(d: dict) -> str:
    name = d["name"]
    lines = [f"[mcp_servers.{name}]"] if re.fullmatch(r"[A-Za-z0-9_-]+", name) else [f'[mcp_servers.{_toml_str(name)[1:-1]}]']
    if d["transport"] == "http":
        lines.append(f"url = {_toml_str(d['url'])}")
        if d["bearer_var"]:
            lines.append(f"bearer_token_env_var = {_toml_str(d['bearer_var'])}")
    else:
        lines.append(f"command = {_toml_str(d['command'])}")
        if d["args"]:
            lines.append("args = [" + ", ".join(_toml_str(a) for a in d["args"]) + "]")
        if d["env_refs"]:
            lines.append("env_vars = [" + ", ".join(_toml_str(v) for v in d["env_refs"]) + "]")
        if d["env_plain"]:
            lines.append(f"[mcp_servers.{name}.env]")
            for k, v in sorted(d["env_plain"].items()):
                lines.append(f"{k} = {_toml_str(v)}")
    return "\n".join(lines)


def render_managed_region(descs: list) -> str:
    if not descs:
        return ""
    body = "\n\n".join(codex_table_from_desc(d) for d in sorted(descs, key=lambda d: d["name"]))
    return f"{MANAGED_BEGIN}\n# 由 memsync 自 Claude Code 同步生成・全量重生・手改會被覆蓋\n{body}\n{MANAGED_END}"


def upsert_managed_region(text: str, region: str) -> str:
    span = _managed_span(text)
    if span:
        new = text[:span[0]].rstrip("\n") + ("\n\n" + region if region else "") + text[span[1]:]
        return new.rstrip("\n") + "\n" if new.strip() else ""
    if not region:
        return text
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + region + "\n"


# ---------- 機密外抽 ----------

def scan_secrets_claude(servers: dict):
    """回 [(server, 位置, key, value, 改寫後值)]。"""
    found = []
    for name, cfg in servers.items():
        for k, v in (cfg.get("env") or {}).items():
            if is_secret(k, v):
                found.append((name, "env", k, v, f"${{{k}}}"))
        for k, v in (cfg.get("headers") or {}).items():
            if is_secret(k, v):
                var = _varname(f"{name}_{k}")
                mm = re.match(r"^(Bearer\s+)(\S+)$", v or "")
                newv = f"Bearer ${{{var}}}" if mm else f"${{{var}}}"
                secret = mm.group(2) if mm else v
                found.append((name, "headers", k, secret, newv))
    return found


def scan_secrets_codex(servers: dict):
    """回 [(server, key, value)]——codex 側 env 明文機密（改寫＝移到 env_vars）。"""
    found = []
    for name, cfg in servers.items():
        for k, v in (cfg.get("env") or {}).items():
            if is_secret(k, v):
                found.append((name, k, v))
        bt = cfg.get("bearer_token")
        if isinstance(bt, str) and bt:
            found.append((name, "__bearer__", bt))
    return found


def load_env_file(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)='(.*)'\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).replace("'\\''", "'")
    return out


def write_env_file(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# memsync MCP 共用機密檔——勿共用此資料夾、勿入 git；值只存在這裡",
             "# shell profile 加：[ -f \"%s\" ] && source \"%s\"" % (path, path)]
    for k in sorted(values):
        v = values[k].replace("'", "'\\''")
        lines.append(f"export {k}='{v}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def rewrite_claude_secrets(cfg_all: dict, found: list) -> str:
    """回改寫後整份 JSON 文字（不落地，由呼叫端驗證後寫）。"""
    for name, loc, k, _v, newv in found:
        cfg_all["mcpServers"][name][loc][k] = newv
    return json.dumps(cfg_all, ensure_ascii=False, indent=2) + "\n"


def _server_section_span(lines: list, name: str):
    """[mcp_servers.<name>] 主表頭起、到下一個非其子表的表頭止（含子表）。"""
    hdr = re.compile(r"^\s*\[mcp_servers\.%s(\.[A-Za-z0-9_.-]+)?\]\s*$" % re.escape(name))
    any_hdr = re.compile(r"^\s*\[")
    start = end = None
    for i, line in enumerate(lines):
        if hdr.match(line) and (start is None):
            start = i
        elif start is not None and any_hdr.match(line) and not hdr.match(line):
            end = i
            break
    return (start, end if end is not None else len(lines)) if start is not None else None


def rewrite_codex_secrets(text: str, found: list) -> str:
    """text 層級改寫：刪明文 env 行、併入 env_vars。改寫後由呼叫端 tomllib 重解析驗證。"""
    lines = text.split("\n")
    by_server: dict = {}
    for name, k, _v in found:
        by_server.setdefault(name, []).append(k)
    for name, keys in by_server.items():
        span = _server_section_span(lines, name)
        if not span:
            continue
        s, e = span
        seg = lines[s:e]
        out, in_env = [], False
        env_vars_line = None
        for line in seg:
            if re.match(r"^\s*\[mcp_servers\.%s\.env\]\s*$" % re.escape(name), line):
                in_env = True
                out.append(line)
                continue
            if re.match(r"^\s*\[", line):
                in_env = False
            key_m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
            if in_env and key_m and key_m.group(1) in keys:
                continue  # 刪明文機密行
            if key_m and key_m.group(1) == "bearer_token" and "__bearer__" in keys:
                out.append(f"bearer_token_env_var = {_toml_str(_varname(name + '_BEARER'))}")
                continue
            if key_m and key_m.group(1) == "env_vars":
                env_vars_line = len(out)
            # 行內 env = { ... }：逐鍵重組
            inline = re.match(r"^(\s*)env\s*=\s*\{(.*)\}\s*$", line)
            if inline:
                kept = []
                for part in re.findall(r'([A-Za-z0-9_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"', inline.group(2)):
                    if part[0] not in keys:
                        kept.append(f'{part[0]} = "{part[1]}"')
                out.append(f"{inline.group(1)}env = {{ {', '.join(kept)} }}" if kept else None)
                if out[-1] is None:
                    out.pop()
                continue
            out.append(line)
        real_keys = [k for k in keys if k != "__bearer__"]
        if real_keys:
            merged = sorted(set(real_keys))
            if env_vars_line is not None:
                old = re.findall(r'"((?:[^"\\]|\\.)*)"', out[env_vars_line])
                merged = sorted(set(old) | set(merged))
                out[env_vars_line] = "env_vars = [" + ", ".join(_toml_str(v) for v in merged) + "]"
            else:
                # 插在主表頭後
                for i, line in enumerate(out):
                    if re.match(r"^\s*\[mcp_servers\.%s\]\s*$" % re.escape(name), line):
                        out.insert(i + 1, "env_vars = [" + ", ".join(_toml_str(v) for v in merged) + "]")
                        break
        # 移除空掉的 env 子表頭
        out2 = []
        for i, line in enumerate(out):
            if re.match(r"^\s*\[mcp_servers\.%s\.env\]\s*$" % re.escape(name), line):
                nxt = next((l for l in out[i + 1:] if l.strip()), "")
                if not nxt or re.match(r"^\s*\[", nxt):
                    continue
            out2.append(line)
        lines[s:e] = out2
    return "\n".join(lines)


def backup(path: Path, ts: str) -> Path:
    b = path.with_name(path.name + f".memsync-bak-{ts}")
    shutil.copy2(path, b)
    return b


def validate_claude(text: str, found: list) -> str:
    """重解析＋語意檢查。回錯誤字串（空＝通過）。"""
    try:
        cfg = json.loads(text)
    except Exception as e:
        return f"JSON 重解析失敗：{e}"
    for name, loc, k, v, _newv in found:
        cur = ((cfg.get("mcpServers") or {}).get(name) or {}).get(loc, {}).get(k, "")
        if isinstance(cur, str) and v in cur:
            return f"{name}.{loc}.{k} 明文仍在"
    return ""


def validate_codex(text: str, found: list) -> str:
    try:
        cfg = tomllib.loads(text)
    except Exception as e:
        return f"TOML 重解析失敗：{e}"
    servers = cfg.get("mcp_servers") or {}
    for name, k, v in found:
        s = servers.get(name) or {}
        if k == "__bearer__":
            if s.get("bearer_token"):
                return f"{name}.bearer_token 明文仍在"
            continue
        if (s.get("env") or {}).get(k):
            return f"{name}.env.{k} 明文仍在"
        if k not in (s.get("env_vars") or []):
            return f"{name}.env_vars 缺 {k}"
    return ""


def mask(v: str) -> str:
    return (v[:3] + "…" + v[-2:]) if isinstance(v, str) and len(v) > 8 else "****"
