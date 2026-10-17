#!/usr/bin/env python3
"""🕊️ feige 适配器：Claude Code Stop hook → 会话收尾战报卡。

注册（用户自行贴到 ~/.claude/settings.json，见 README）：

    "hooks": { "Stop": [ { "hooks": [ {
        "type": "command",
        "command": "python <repo>/adapters/claude-code/stop_notify.py" } ] } ] }

payload 经 stdin JSON：`session_id` / `transcript_path`（jsonl 转录）/ `cwd`。
transcript schema 已拿本机真实转录（~/.claude/projects/**）验证：
- 行 type=="assistant"；isSidechain==true 为子代理，跳过（对齐 ZCode 非 main 口径）；
- message.content 数组按 part.type 分：text（teaser 来源，取最后一条）/
  thinking（💭 计数）/ tool_use（🔧 计数）；
- 模型 message.model；上下文水位 = 最后一条 assistant 的
  message.usage.input_tokens + cache_creation_input_tokens + cache_read_input_tokens；
- 分轮：最后一条真实用户提问（type=="user" 且非 tool_result/meta/sidechain）之后为
  本轮；💭/🔧/🎫/⏱️ 只算本轮（🎫 = 本轮 assistant 行 usage.output_tokens 之和；
  ⏱️ = 提问时刻 → 文件末条）。没有提问行的转录退化为全文件统计。

开关与 fail-open 约定见 adapters/common.py（FEIGE_HOOK_NOTIFY=1 启用，默认关）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (body_for_card, compact, fmt_elapsed, log, project_label,  # noqa: E402
                    read_payload, run, send_card, stats_scope)

# 常见模型上下文窗口表（单位 token）；未知模型退化为绝对水位 compact(ctx)
KNOWN_CONTEXT_WINDOWS: list[tuple[re.Pattern, int]] = [
    # Gemini 系列 (1.5 / 2.0 / 2.5 / 3.0 / 3.8 / flash / pro)
    (re.compile(r"gemini-(?:1\.5-pro|2\.\d+-pro|3\.\d+-pro)", re.I), 2_097_152),
    (re.compile(r"gemini", re.I), 1_048_576),
    # Claude 系列
    (re.compile(r"claude-(?:fable-5|opus-5|sonnet-5|haiku-4-5)", re.I), 1_048_576),
    (re.compile(r"claude-.*-1m", re.I), 1_048_576),
    (re.compile(r"claude-(?:3|4|sonnet-4|opus-4|haiku-4)", re.I), 200_000),
    (re.compile(r"claude", re.I), 200_000),
    # OpenAI 系列
    (re.compile(r"gpt-4o(?:-mini)?", re.I), 128_000),
    (re.compile(r"gpt-4-turbo", re.I), 128_000),
    (re.compile(r"gpt-5", re.I), 400_000),
    (re.compile(r"o[134](?:-mini)?", re.I), 200_000),
    # Kimi
    (re.compile(r"kimi-k3|k3\b", re.I), 1_048_576),
    (re.compile(r"kimi-for-coding", re.I), 262_144),
    # DeepSeek / Qwen / GLM
    (re.compile(r"deepseek", re.I), 131_072),
    (re.compile(r"qwen", re.I), 131_072),
    (re.compile(r"glm", re.I), 128_000),
]


def context_window(model: str) -> int:
    """根据模型名称解析上下文窗口（token 数）；未知返回 0。支持环境变量覆盖。"""
    for env_var in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "FEIGE_CONTEXT_TOTAL"):
        val = (os.environ.get(env_var) or "").strip()
        if val.isdigit() and int(val) > 0:
            return int(val)
    if not model:
        return 0
    raw = model.split("·")[-1].strip().split("/")[-1].strip()
    for pat, limit in KNOWN_CONTEXT_WINDOWS:
        if pat.search(raw):
            return limit
    return 0


def fmt_context(ctx: int, limit: int) -> str:
    """上下文水位：有分母 `213.7k/1.0m (21%)`，无分母退化为 `213.7k`。"""
    if not ctx:
        return ""
    if not limit:
        return compact(ctx)
    return f"{compact(ctx)}/{compact(limit)} ({round(100 * ctx / limit)}%)"


def _is_user_turn_start(entry: dict) -> bool:
    """真实用户提问 = 一轮的开始；tool_result/meta/sidechain/压缩摘要都不算。"""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {p.get("type") for p in content if isinstance(p, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def summarize_transcript(path: str) -> dict | None:
    """解析 Claude Code 转录：teaser + 最后一轮统计；拿不到就 None/省略，绝不抛。"""
    first_ts = last_ts = turn_ts = None
    model = ""
    thinking_turns = 0
    tools_total = 0
    tokens_total = 0
    all_thinking = 0
    all_tools = 0
    all_tokens = 0
    ctx = 0
    last_text = ""
    saw_assistant = False

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
            ts = entry.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if _is_user_turn_start(entry):
                # 新一轮开始：本轮统计清零重置，最后一轮的提问时刻记为 ⏱️ 起点
                turn_ts = ts or turn_ts
                thinking_turns = tools_total = tokens_total = 0
                continue
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            saw_assistant = True
            msg = entry.get("message") or {}
            model = msg.get("model") or model
            parts = msg.get("content") or []
            if not isinstance(parts, list):
                continue
            has_thinking = False
            for part in parts:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "thinking":
                    has_thinking = True
                elif ptype == "tool_use":
                    tools_total += 1
                    all_tools += 1
                elif ptype == "text":
                    text = str(part.get("text") or "").strip()
                    if text:
                        last_text = text
            if has_thinking:
                thinking_turns += 1
                all_thinking += 1
            usage = msg.get("usage") or {}
            try:
                # 三段都算上下文：未缓存输入 + 本轮新写缓存 + 命中缓存（漏掉 creation 的话
                # 首轮几乎全是新写缓存，水位会显示成个位数）
                used = sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                ctx = used or ctx  # 最后一条 assistant 的水位 ≈ 当前上下文
            except (TypeError, ValueError):
                pass
            try:
                out = int(usage.get("output_tokens") or 0)
                tokens_total += out
                all_tokens += out
            except (TypeError, ValueError):
                pass

    if not saw_assistant:
        return None

    if turn_ts is None or stats_scope() == "session":  # 全程口径 / 没有提问行：全文件统计
        thinking_turns, tools_total, tokens_total = all_thinking, all_tools, all_tokens
        turn_ts = None

    elapsed = ""
    try:
        t0 = datetime.fromisoformat(str(turn_ts or first_ts).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
        if t1 >= t0:
            elapsed = fmt_elapsed((t1 - t0).total_seconds())
    except (ValueError, TypeError):
        pass

    return {
        "body": body_for_card(last_text),
        # 约定：CLI/Mirasim 场景模型名前带工具名
        "model": f"claude-code · {model}" if model else "claude-code",
        "thinking": thinking_turns,
        "tools": tools_total,
        "tokens": tokens_total,
        # 上下文水位：已知模型/环境变量给分母 `213.7k/1.0m (21%)`，未知退化为 `213.7k`
        "context": fmt_context(ctx, context_window(model)),
        "elapsed": elapsed,
    }


def main(payload: dict | None = None) -> None:
    """payload 缺省从 stdin 读；hooks/stop.py 分发时直接传入（stdin 已被它读走）。"""
    payload = read_payload() if payload is None else payload
    project = project_label(str(payload.get("cwd") or ""))
    transcript = str(payload.get("transcript_path") or "")
    if not transcript or not Path(transcript).is_file():
        log(f"transcript not found: {transcript or '(missing in payload)'}")
        return
    summary = summarize_transcript(transcript)
    if not summary:
        log(f"[{Path(transcript).name}] no assistant entries")
        return
    stats = {
        "project": project,
        "model": summary["model"],
        "thinking": summary["thinking"],
        "tools": summary["tools"],
        "context": summary["context"],
        "tokens": summary["tokens"],
        "elapsed": summary["elapsed"],
    }
    send_card("", summary["body"], stats, agent="Claude Code",
              session=str(payload.get("session_id") or ""))


if __name__ == "__main__":
    run(main)
