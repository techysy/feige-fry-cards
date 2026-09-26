#!/usr/bin/env python3
"""🕊️ feige 统一 Stop hook 入口：一份 hooks/hooks.json，四个宿主共用。

Claude Code、Codex、ZCode 都自动加载插件根目录的 hooks/hooks.json（Mirasim 复用前两者
的插件缓存），且都认 Claude 标准 `type: "command"` + `${CLAUDE_PLUGIN_ROOT}`。所以只挂
这一个脚本，由它识别宿主再分发给各自的适配器：

    ZCode        环境里有 ZCODE_SESSION_ID / ZCODE_PLUGIN_ROOT（ZCode 注入）
                 → hooks/stop-notify.mjs（解析 ~/.zcode/cli/rollout）
    kimi-code    payload 带 client_type: kimi_code_*（kimi-code 不读 hooks.json，
                 需在 ~/.kimi-code/config.toml 手挂，可直达本脚本或适配器）
                 → adapters/kimi-code/stop_notify.py（解析 agents/main/wire.jsonl；
                 注册即开启，不吃 FEIGE_HOOK_NOTIFY 默认关）
    Codex        payload 带 turn_id（Codex 扩展字段），或转录在 .codex/ 下
                 → adapters/codex/notify.py main_hook（直接取 last_assistant_message）
    Claude Code  其余
                 → adapters/claude-code/stop_notify.py（解析 transcript_path）

协议：stdin 读一个 JSON 事件；stdout 不写任何内容（宿主会把 Stop hook 的 stdout 当
hook 输出 JSON 解析）；诊断只走 stderr；任何异常 exit 0，绝不阻塞或拖死 agent。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
ADAPTERS = HOOKS.parent / "adapters"


def log(msg: str) -> None:
    try:
        print(f"[feige] {msg}", file=sys.stderr, flush=True)
    except (ValueError, OSError):
        pass


def detect_host(payload: dict, env=None) -> str:
    env = os.environ if env is None else env
    if env.get("ZCODE_SESSION_ID") or env.get("ZCODE_PLUGIN_ROOT"):
        return "zcode"
    if str(payload.get("client_type") or "").startswith("kimi"):
        return "kimi-code"
    transcript = str(payload.get("transcript_path") or "").replace("\\", "/")
    if "turn_id" in payload or "/.codex/" in transcript:
        return "codex"
    return "claude-code"


def _load(path: Path, name: str):
    """按路径加载适配器（claude-code 目录名带连字符，不能当包 import）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_zcode(raw: str) -> None:
    """ZCode 的 rollout 解析在 mjs 里（与 zcode-feishu-bridge 同口径），原样转交 payload。

    开关（FEIGE_HOOK_NOTIFY / 插件 hook_notify）与后台发送都由 mjs 自己处理。
    """
    try:
        subprocess.run(["node", str(HOOKS / "stop-notify.mjs")], input=raw.encode("utf-8"),
                       stdout=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"zcode hook needs node: {exc}")


def main() -> None:
    try:
        raw = sys.stdin.read() or ""
    except (ValueError, OSError):
        raw = ""
    try:
        payload = json.loads(raw.strip() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    host = detect_host(payload)
    if host == "zcode":
        run_zcode(raw)
        return
    sys.path.insert(0, str(ADAPTERS))
    import common  # 与适配器里的 `from common import …` 共用同一模块实例

    if host == "codex":
        adapter = _load(ADAPTERS / "codex" / "notify.py", "feige_codex")
        common.run(lambda: adapter.main_hook(payload))
    elif host == "kimi-code":
        # kimi 适配器自带开关（注册即开启），不走 common.run 的 FEIGE_HOOK_NOTIFY 默认关
        adapter = _load(ADAPTERS / "kimi-code" / "stop_notify.py", "feige_kimi_code")
        try:
            common.apply_plugin_options()
            adapter.main(payload)
        except Exception as exc:
            log(f"kimi adapter failed (fail-open): {exc}")
    else:
        adapter = _load(ADAPTERS / "claude-code" / "stop_notify.py", "feige_claude_code")
        common.run(lambda: adapter.main(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail-open
        log(f"stop hook failed (fail-open): {exc}")
    sys.exit(0)
