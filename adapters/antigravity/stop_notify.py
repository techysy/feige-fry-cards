#!/usr/bin/env python3
"""🕊️ feige 适配器：Antigravity CLI（agy）Stop hook → 会话收尾战报卡。

agy 原生支持 hooks（`hooks.json`，`Stop` 事件，payload 走 stdin JSON、camelCase）。
hook payload 已拿本机 agy 1.5.11 实证（`agy -p` + dump stdin hook）：

    {"conversationId": "...", "cwd": "...", "modelName": "gemini-3.8-flash-high",
     "transcriptPath": "~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl",
     "hookEventName": "Stop", "terminationReason": "NO_TOOL_CALL", "fullyIdle": true, ...}

等价于 Claude transcript 的是 `transcriptPath`（JSONL，schema 同样实证）：

- 行 type=="PLANNER_RESPONSE"：模型（`model`）与用量（`input_tokens` /
  `cache_read_tokens` / `output_tokens`）；`thinking` 非空计 💭；`tool_calls`
  列表逐项计 🔧；`content` 为 teaser 来源（取最后一条非空）；
- 上下文水位 = 最后一条 PLANNER_RESPONSE 的 input_tokens + cache_read_tokens
  （agy 不暴露 cache_creation）；
- 分轮：最后一条 `type=="USER_INPUT"` 之后为本轮；💭/🔧/🎫/⏱️ 只算本轮
  （⏱️ = 本轮首条 created_at → 末条 created_at）；没有提问行的转录退化为全文件统计。
  `GENERIC`（工具执行结果）/`SYSTEM_MESSAGE`（系统注入）行不是提问，不清零。

注册（用户自行贴到 `<workspace>/.agents/hooks.json` 或 `~/.gemini/config/hooks.json`，
后者全局生效；见 README）：

    {
      "feige": {
        "Stop": [
          {
            "type": "command",
            "command": "python \"<repo>/adapters/antigravity/stop_notify.py\"",
            "timeout": 20
          }
        ]
      }
    }

开关与 fail-open 约定见 adapters/common.py（FEIGE_HOOK_NOTIFY=1 启用，默认关）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (body_for_card, compact, fmt_elapsed, log, project_label,  # noqa: E402
                    read_payload, run, send_card, stats_scope)

# agy 内置模型上下文窗口（`agy models`）；未收录退化为绝对水位 compact(ctx)。
# Flash 系列 1.0m / Claude 200k / GPT-OSS 128k，与其他适配器的窗口表同源。
KNOWN_CONTEXT_WINDOWS = {
    "gemini-3.8-flash": 1_048_576,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "gpt-oss-120b": 131_072,
}


def context_window(model: str) -> int:
    """模型上下文窗口：环境变量覆盖 → 已知表前缀匹配 → 0（未知）。"""
    for env_var in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "FEIGE_CONTEXT_TOTAL"):
        val = (os.environ.get(env_var) or "").strip()
        if val.isdigit() and int(val) > 0:
            return int(val)
    if not model:
        return 0
    for prefix, limit in KNOWN_CONTEXT_WINDOWS.items():
        if model.startswith(prefix):
            return limit
    return 0


def fmt_context(ctx: int, limit: int) -> str:
    """上下文水位：有分母 `153.6k/1.0m (15%)`，无分母退化为 `153.6k`。"""
    if not ctx:
        return ""
    if not limit:
        return compact(ctx)
    return f"{compact(ctx)}/{compact(limit)} ({round(100 * ctx / limit)}%)"


def _parse_ts(ts: str) -> datetime | None:
    """agy 转录时间戳（ISO8601，UTC `Z` 或带本地时区）；解析失败 None，绝不抛。"""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def summarize_transcript(path: str) -> dict | None:
    """解析 agy transcript_full.jsonl：本轮收尾正文 + 本轮统计；拿不到就 None，绝不抛。

    正文口径与 Claude 适配器一致：只取当前轮（最后一条 USER_INPUT 之后）的最后一条
    非空 PLANNER_RESPONSE text——上一轮的旧文不回落，当前轮没有文本收尾就发占位符。
    """
    first_dt = last_dt = turn_dt = None
    model = ""
    thinking_turns = 0
    tools_total = 0
    tokens_total = 0
    all_thinking = 0
    all_tools = 0
    all_tokens = 0
    ctx = 0
    all_last_text = ""
    ctx = 0
    last_text = ""
    saw_model = False

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
                entry = json.loads(line)
            except ValueError:
                continue  # 脏行跳过，fail-open
            etype = entry.get("type")
            dt = _parse_ts(entry.get("created_at"))
            if dt:
                first_dt = first_dt or dt
                last_dt = dt
            if etype == "USER_INPUT":  # 本轮起点：统计清零（GENERIC/SYSTEM_MESSAGE 不是提问）
                turn_dt = dt or turn_dt
                thinking_turns = tools_total = tokens_total = 0
                last_text = ""
                continue
            if etype != "PLANNER_RESPONSE":
                continue
            saw_model = True
            model = str(entry.get("model") or "") or model
            try:
                # agy 不暴露 cache_creation：未缓存输入 + 命中缓存 ≈ 当前上下文
                used = int(entry.get("input_tokens") or 0) + int(entry.get("cache_read_tokens") or 0)
                ctx = used or ctx
            except (TypeError, ValueError):
                pass
            try:
                out = int(entry.get("output_tokens") or 0)
                tokens_total += out
                all_tokens += out
            except (TypeError, ValueError):
                pass
            if str(entry.get("thinking") or "").strip():
                thinking_turns += 1
                all_thinking += 1
            for _call in entry.get("tool_calls") or []:
                tools_total += 1
                all_tools += 1
            text = str(entry.get("content") or "").strip()
            if text:
                last_text = text
                all_last_text = text

    if not saw_model:
        return None

    if turn_dt is None or stats_scope() == "session":  # 全程口径 / 没有提问行：全文件统计
        thinking_turns, tools_total, tokens_total = all_thinking, all_tools, all_tokens
        last_text = all_last_text
        turn_dt = None

    elapsed = ""
    try:
        t0 = turn_dt or first_dt
        t1 = last_dt
        if t0 and t1 and t1 >= t0:
            elapsed = fmt_elapsed((t1 - t0).total_seconds())
    except (TypeError, ValueError):
        pass

    return {
        "body": body_for_card(last_text),
        # 约定：CLI/Mirasim 场景模型名前带工具名
        "model": f"antigravity · {model}" if model else "antigravity",
        "raw_model": model,
        "thinking": thinking_turns,
        "tools": tools_total,
        "tokens": tokens_total,
        "ctx": ctx,
        "elapsed": elapsed,
    }


def main(payload: dict | None = None) -> None:
    """payload 缺省从 stdin 读；hooks/stop.py 分发时直接传入（stdin 已被它读走）。"""
    payload = read_payload() if payload is None else payload
    project = project_label(str(payload.get("cwd") or ""))
    transcript = str(payload.get("transcriptPath") or payload.get("transcript_path") or "")
    if not transcript or not Path(transcript).is_file():
        log(f"transcript not found: {transcript or '(missing in payload)'}")
        return
    summary = summarize_transcript(transcript)
    if not summary:
        log(f"[{Path(transcript).name}] no planner responses")
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
    send_card("", summary["body"], stats, agent="Antigravity",
              session=str(payload.get("conversationId") or ""),
              # 传转录路径：后台发送进程开卡前重读一次，治宿主落盘滞后
              transcript=transcript)


if __name__ == "__main__":
    run(main)
