"""🕊️ feige-fry-cards 适配器公共件：开关、项目标签、teaser、fail-open 发送。

约定（与 M2 ZCode hook 一致）：
- FEIGE_HOOK_NOTIFY=1 才启用，默认关闭；
- FEIGE_DRY_RUN=1 把卡片 JSON 打到 stdout，不发网络；
- 全部 fail-open：任何异常 → stderr 打日志 → exit 0，绝不拖死 agent。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# feige.py 在仓库根（adapters/ 的上一级）
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import feige  # noqa: E402

TEASER_CHARS = 300  # 摘要是战报不是镜像：正文 ≤300 字
_ON = ("1", "true", "yes")


def log(msg: str) -> None:
    """诊断只走 stderr；stderr 被关了也不许炸。"""
    try:
        print(f"[feige] {msg}", file=sys.stderr, flush=True)
    except (ValueError, OSError):
        pass


def hook_enabled() -> bool:
    return (os.environ.get("FEIGE_HOOK_NOTIFY") or "").strip().lower() in _ON


def dry_run() -> bool:
    return (os.environ.get("FEIGE_DRY_RUN") or "").strip().lower() in _ON


def teaser(text: str, limit: int = TEASER_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ……"


def compact(n: int | float) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def project_label(cwd: str) -> str:
    """从 cwd 提取项目标签：GitHub Files\\X / workspace\\X 一级目录，否则目录末段。"""
    p = (cwd or "").strip().rstrip("/\\").replace("/", "\\")
    if not p:
        return ""
    for marker in ("GitHub Files\\", "workspace\\"):
        i = p.rfind(marker)
        if i >= 0:
            seg = p[i + len(marker):].split("\\")[0]
            if seg and not seg.endswith(":"):
                return seg
    seg = p.split("\\")[-1]
    return seg if len(seg) > 1 and not seg.endswith(":") else ""


def read_payload() -> dict:
    """适配器 payload：优先 argv 最后一个参数（Codex notify 风格），否则 stdin。"""
    raw = ""
    if len(sys.argv) > 1:
        candidate = sys.argv[-1].strip()
        if candidate.startswith("{"):
            raw = candidate
    if not raw:
        try:
            raw = sys.stdin.read() or ""
        except (ValueError, OSError):
            raw = ""
    try:
        data = json.loads(raw.strip() or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def send_card(title: str, body: str, stats: dict, agent: str) -> None:
    """统一发送：dry-run 或渠道未配置时把卡片 JSON 打 stdout，否则真发；失败只落日志。"""
    full_title = f"{stats.get('project') + ' · ' if stats.get('project') else ''}{agent} 任务完成"
    title = title or full_title
    preview = dry_run()
    if not preview and not feige.pick_channel():
        log("no channel configured (webhook/app creds missing), preview only")
        preview = True
    ok, detail = feige.send_report(title, body, stats=stats, status="ok",
                                   dry_run=preview)
    if preview:
        try:
            print(detail, flush=True)  # dry-run：卡片 JSON 打 stdout
        except (ValueError, OSError):
            pass
    elif ok:
        log(f"sent: {detail}")
    else:
        log(f"send failed (fail-open): {detail}")


def run(main) -> None:
    """驱动入口：默认关 directly return；任何异常 exit 0。"""
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not hook_enabled():
        return
    try:
        main()
    except Exception:
        log(f"adapter failed (fail-open):\n{traceback.format_exc()}")
