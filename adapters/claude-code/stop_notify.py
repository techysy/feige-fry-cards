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
  message.usage.input_tokens + cache_creation_input_tokens + cache_read_input_tokens；🎫 为所有
  assistant 行的 message.usage.output_tokens 累加；
- ⏱️ = 全文件首末 timestamp 之差。

开关与 fail-open 约定见 adapters/common.py（FEIGE_HOOK_NOTIFY=1 启用，默认关）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import compact, fmt_elapsed, log, project_label, read_payload, run, send_card, teaser  # noqa: E402


def summarize_transcript(path: str) -> dict | None:
    """解析 Claude Code 转录：teaser + 全文件统计；拿不到就 None/省略，绝不抛。"""
    first_ts = last_ts = None
    model = ""
    thinking_turns = 0
    tools_total = 0
    tokens_total = 0
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
                elif ptype == "text":
                    text = str(part.get("text") or "").strip()
                    if text:
                        last_text = text
            if has_thinking:
                thinking_turns += 1
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
                tokens_total += int(usage.get("output_tokens") or 0)
            except (TypeError, ValueError):
                pass

    if not saw_assistant:
        return None

    elapsed = ""
    try:
        t0 = datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
        if t1 >= t0:
            elapsed = fmt_elapsed((t1 - t0).total_seconds())
    except (ValueError, TypeError):
        pass

    return {
        "body": teaser(last_text) or "……（本轮无文本输出）",
        "model": model,
        "thinking": thinking_turns,
        "tools": tools_total,
        "tokens": tokens_total,
        # Claude 上下文窗口因模型/计划而异，硬编码百分比不靠谱——只报绝对水位
        "context": compact(ctx) if ctx else "",
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
