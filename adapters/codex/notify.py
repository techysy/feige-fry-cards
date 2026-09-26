#!/usr/bin/env python3
"""🕊️ feige 适配器：Codex CLI → 会话收尾战报卡。两个入口：

1. **插件 Stop hook**（推荐，Codex ≥0.15x 插件系统）：hooks/stop.py 识别出 Codex 后调
   `main_hook(payload)`。payload 与 Claude Code 同构（已拿本机 codex.exe 内置 schema
   实证）：`session_id` / `turn_id`（Codex 扩展）/ `cwd` / `model` / `transcript_path` /
   `last_assistant_message`。模型与最后一条回复直接取 payload，不解析 Codex 转录。

2. **legacy notify**（用户自行贴到 ~/.codex/config.toml）：

       notify = ["python", "<repo>/adapters/codex/notify.py"]

   notify 程序以**最后一个 argv** 收到 JSON 事件，字段 kebab-case：`type` / `thread-id` /
   `turn-id` / `cwd` / `client` / `input-messages` / `last-assistant-message`。只在
   type=="agent-turn-complete" 时发卡。

   **包装链**：Codex 只有一条 notify 命令。已有 notify（如 codex-computer-use）时，把原
   命令原样接在本脚本后面即可两边都跑：

       notify = ["python", "<repo>/adapters/codex/notify.py", "<原命令>", "<原参数>…"]

   本脚本先以「原命令 + 事件 JSON」启动原 notify（行为与直接配置一致），再发飞鸽卡，
   最后等原命令结束并透传其退出码。飞鸽的开关/失败都不影响原命令。

Codex 不给 token/工具统计——卡片带 teaser + 项目 + 模型，其余字段按约定省略，绝不硬编。
开关与 fail-open 约定见 adapters/common.py（FEIGE_HOOK_NOTIFY=1 启用，默认关）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import log, project_label, read_payload, run, send_card, teaser  # noqa: E402


def codex_model() -> str:
    """模型取 Codex config.toml 顶层 `model = "..."`；读不到返回空串（fail-open）。"""
    cfg = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"
    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r'^model\s*=\s*"([^"]+)"', line.strip())
            if m:
                return m.group(1)
            if line.strip().startswith("["):  # 进入表段后就不算顶层了
                break
    except OSError:
        pass
    return ""


def _send(text: str, cwd: str, model: str, session: str) -> None:
    stats = {
        "project": project_label(cwd),
        "model": model or codex_model(),
        # token/工具/耗时统计 Codex 不给——按约定省略，不硬编
    }
    send_card("", teaser(text) or "……（本轮无文本输出）", stats, agent="Codex", session=session)


def main_hook(payload: dict) -> None:
    """插件 Stop hook 入口（snake_case payload）。"""
    _send(str(payload.get("last_assistant_message") or "").strip(),
          str(payload.get("cwd") or ""), str(payload.get("model") or ""),
          str(payload.get("session_id") or ""))


def main(payload: dict | None = None) -> None:
    """legacy notify 入口（kebab-case 事件，最后一个 argv）。"""
    payload = read_payload() if payload is None else payload
    etype = payload.get("type") or "agent-turn-complete"  # 无 type 的老版本按完成处理
    if etype != "agent-turn-complete":
        log(f"skip event type: {etype}")
        return
    _send(str(payload.get("last-assistant-message") or "").strip(),
          str(payload.get("cwd") or ""), "", str(payload.get("thread-id") or ""))


def chain_start(argv: list[str]) -> subprocess.Popen | None:
    """包装链：argv 形如 [<原命令>, <原参数>…, <事件 JSON>] 时启动原 notify。

    只有事件 JSON（直接配置本脚本）时返回 None。启动失败只落日志——飞鸽不背锅，
    也不因此漏发自己的卡。
    """
    if len(argv) < 2 or not argv[-1].lstrip().startswith("{"):
        return None
    try:
        return subprocess.Popen(argv)
    except OSError as exc:
        log(f"chained notify failed to start: {argv[0]}: {exc}")
        return None


if __name__ == "__main__":
    chained = chain_start(sys.argv[1:])
    run(main)
    if chained is not None:
        sys.exit(chained.wait())
