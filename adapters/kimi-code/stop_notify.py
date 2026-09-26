#!/usr/bin/env python3
"""🕊️ feige 适配器：kimi-code Stop hook → 会话收尾战报卡。

kimi-code 不自动加载插件 hooks.json，需在 ~/.kimi-code/config.toml 手挂：

    [[hooks]]
    event = "Stop"
    command = 'python "<repo>/adapters/kimi-code/stop_notify.py"'
    timeout = 20

payload 经 stdin JSON：`hook_event_name` / `session_id` / `session_title` /
`client_type`（kimi_code_cli）/ `cwd`。等价于 Claude transcript 的是
$KIMI_CODE_HOME/sessions/<wd>/<session_id>/agents/main/wire.jsonl（append-only JSONL）：

- `usage.record`：🎫 = usage.output 累加；上下文水位 = 最后一条的
  inputOther + inputCacheRead + inputCacheCreation；
- `llm.request`：真实模型名（跳过 __kimi_env_model__ / agent-loop 占位）；
- `context.append_loop_event`：tool.call 计 🔧；content.part 的 think 计 💭、
  text 为 teaser 来源（取最后一条）；
- `event.turnId`：⏱️ 报单轮用时 = 全文件末条时间 − 最后一轮首条时间
  （多轮会话的累计墙钟没有行动意义，与 Claude/Codex 口径刻意不同）。

上下文分母：已知模型窗口表（k3=1.0m 等）→ kimi config.toml [models.*] max_context_size
（含 overrides）→ 都没有则退化为绝对水位。形如 `153.6k/1.0m (15%)`。

开关：kimi-code 手挂注册即 opt-in，故默认开启；FEIGE_HOOK_NOTIFY=0 显式关闭，
FEIGE_DRY_RUN=1 只预览不发。全程 fail-open（见 adapters/common.py）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import compact, fmt_elapsed, log, project_label, read_payload, send_card, teaser  # noqa: E402

_OFF = ("0", "false", "no", "off")
_MODEL_PLACEHOLDERS = {"__kimi_env_model__", "agent-loop"}

# kimi-code 已知模型上下文窗口（官方配置文档）；未知模型走 config.toml 或绝对水位。
KNOWN_CONTEXT_WINDOWS = {
    "k3": 1048576,
    "kimi-k3": 1048576,
    "kimi-for-coding": 262144,
    "kimi-for-coding-highspeed": 262144,
}


def hook_enabled() -> bool:
    # 与其他宿主「默认关」相反：kimi-code 手挂注册即开启，显式 0 才关。
    return (os.environ.get("FEIGE_HOOK_NOTIFY") or "1").strip().lower() not in _OFF


def kimi_home() -> Path:
    return Path(os.environ.get("KIMI_CODE_HOME") or (Path.home() / ".kimi-code"))


def find_wire(session_id: str, home: Path | None = None) -> Path | None:
    if not session_id:
        return None
    sessions = (home or kimi_home()) / "sessions"
    try:
        for wd in sessions.iterdir():
            wire = wd / session_id / "agents" / "main" / "wire.jsonl"
            if wire.is_file():
                return wire
    except OSError:
        pass
    return None


def _config_context_window(model: str, home: Path) -> int:
    """kimi config.toml [models.*] 的 max_context_size（overrides 优先）；解析失败 0。"""
    cfg = home / "config.toml"
    try:
        import tomllib
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return 0
    best = 0
    for alias, entry in (data.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        em = str(entry.get("model") or "")
        if not model or not (em == model or alias == model
                             or str(alias).endswith("/" + model) or (em and model.endswith(em))):
            continue
        ov = entry.get("overrides") or {}
        try:
            best = max(best, int(ov.get("max_context_size") or entry.get("max_context_size") or 0))
        except (TypeError, ValueError):
            pass
    return best


def context_window(model: str, home: Path | None = None) -> int:
    """模型上下文窗口：已知表 → config.toml → 0（未知）。"""
    if model in KNOWN_CONTEXT_WINDOWS:
        return KNOWN_CONTEXT_WINDOWS[model]
    return _config_context_window(model, home or kimi_home())


def fmt_context(ctx: int, limit: int) -> str:
    """上下文水位：有分母 `153.6k/1.0m (15%)`，无分母退化为 `153.6k`。"""
    if not ctx:
        return ""
    if not limit:
        return compact(ctx)
    return f"{compact(ctx)}/{compact(limit)} ({round(100 * ctx / limit)}%)"


def summarize_wire(path: Path) -> dict | None:
    """解析 wire.jsonl：teaser + 统计；拿不到返回 None，绝不抛。"""
    first_t = last_t = None
    model = ""
    thinking = 0
    tools = 0
    tokens_total = 0
    ctx = 0
    last_text = ""
    turn_first: dict[str, float] = {}
    last_turn = None
    saw_turn = False

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("time")
            if isinstance(t, (int, float)):
                if first_t is None:
                    first_t = t
                last_t = t
            rtype = rec.get("type")
            if rtype == "usage.record":
                usage = rec.get("usage") or {}
                try:
                    used = sum(int(usage.get(k) or 0) for k in (
                        "inputOther", "inputCacheRead", "inputCacheCreation"))
                    ctx = used or ctx
                    tokens_total += int(usage.get("output") or 0)
                except (TypeError, ValueError):
                    pass
                saw_turn = True
                continue
            if rtype == "llm.request":
                m = str(rec.get("model") or "")
                if m and m not in _MODEL_PLACEHOLDERS:
                    model = m
                continue
            if rtype != "context.append_loop_event":
                continue
            if rec.get("agentId") not in (None, "main"):
                continue
            event = rec.get("event") or {}
            tid = event.get("turnId")
            if tid is not None:
                last_turn = tid
                if isinstance(t, (int, float)) and tid not in turn_first:
                    turn_first[tid] = t
            etype = event.get("type")
            if etype == "tool.call":
                tools += 1
                saw_turn = True
            elif etype == "content.part":
                part = event.get("part") or {}
                ptype = part.get("type")
                if ptype == "think":
                    thinking += 1
                elif ptype == "text":
                    text = str(part.get("text") or "").strip()
                    if text:
                        last_text = text
                        saw_turn = True

    if not saw_turn:
        return None

    # ⏱️ 单轮用时：最后一轮首条 loop 事件 → 全文件末条记录
    elapsed = ""
    if last_turn is not None and last_t is not None and last_turn in turn_first:
        span = (last_t - turn_first[last_turn]) / 1000.0
        if span >= 0:
            elapsed = fmt_elapsed(span)
    elif first_t is not None and last_t is not None and last_t >= first_t:
        elapsed = fmt_elapsed((last_t - first_t) / 1000.0)

    return {
        "body": teaser(last_text) or "……（本轮无文本输出）",
        # 约定：CLI/Mirasim 场景模型名前带工具名
        "model": f"kimi-code · {model}" if model else "kimi-code",
        "raw_model": model,
        "thinking": thinking,
        "tools": tools,
        "tokens": tokens_total,
        "ctx": ctx,
        "elapsed": elapsed,
    }


def main(payload: dict | None = None) -> None:
    """payload 缺省从 stdin 读；hooks/stop.py 分发时直接传入（stdin 已被它读走）。"""
    if not hook_enabled():
        return
    payload = read_payload() if payload is None else payload
    project = project_label(str(payload.get("cwd") or ""))
    session_id = str(payload.get("session_id") or "")
    wire = find_wire(session_id)
    if not wire:
        log(f"wire.jsonl not found for session {session_id or '(missing)'}")
        return
    summary = summarize_wire(wire)
    if not summary:
        log(f"[{wire.name}] no turn records")
        return
    stats = {
        "project": project,
        "model": summary["model"],
        "thinking": summary["thinking"],
        "tools": summary["tools"],
        "context": fmt_context(summary["ctx"], context_window(summary["raw_model"])),
        "tokens": summary["tokens"],
        "elapsed": summary["elapsed"],
    }
    send_card("", summary["body"], stats, agent="kimi-code", session=session_id)


if __name__ == "__main__":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        main()
    except Exception as exc:  # fail-open
        log(f"kimi adapter failed (fail-open): {exc}")
    sys.exit(0)
