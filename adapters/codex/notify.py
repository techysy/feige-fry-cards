#!/usr/bin/env python3
"""🕊️ feige 适配器：Codex CLI notify → 会话收尾战报卡。

注册（用户自行贴到 ~/.codex/config.toml，见 README；Codex 只有一条 notify 命令，
已有 notify 的用户需自行合并成一个包装脚本）：

    notify = ["python", "<repo>/adapters/codex/notify.py"]

调用方式（已拿本机 codex.exe 二进制字符串实证）：notify 程序以**最后一个 argv**
收到一个 JSON 事件，字段为 kebab-case：`type` / `thread-id` / `turn-id` / `cwd` /
`client` / `input-messages` / `last-assistant-message`。只在
type=="agent-turn-complete" 时发卡；其余类型静默 exit 0。

Codex notify 不给 token/工具统计——卡片带 teaser + 项目 + 模型（config.toml 顶层
`model = "..."`，读不到就省略），其余字段按约定省略，绝不硬编。

开关与 fail-open 约定见 adapters/common.py（FEIGE_HOOK_NOTIFY=1 启用，默认关）。
"""

from __future__ import annotations

import os
import re
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


def main() -> None:
    payload = read_payload()
    etype = payload.get("type") or "agent-turn-complete"  # 无 type 的老版本按完成处理
    if etype != "agent-turn-complete":
        log(f"skip event type: {etype}")
        return
    text = str(payload.get("last-assistant-message") or "").strip()
    stats = {
        "project": project_label(str(payload.get("cwd") or "")),
        "model": codex_model(),
        # token/工具/耗时统计 notify 事件里没有——按约定省略，不硬编
    }
    send_card("", teaser(text) or "……（本轮无文本输出）", stats, agent="Codex")


if __name__ == "__main__":
    run(main)
