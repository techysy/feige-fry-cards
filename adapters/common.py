"""🕊️ feige-fry-cards 适配器公共件：开关、项目标签、teaser、fail-open 发送。

约定（与 M2 ZCode hook 一致）：
- FEIGE_HOOK_NOTIFY=1 才启用，默认关闭；
- FEIGE_DRY_RUN=1 把卡片 JSON 打到 stdout，不发网络；
- 正式发送走后台子进程（feige.py send --route），hook 立刻返回；
- FEIGE_DEBOUNCE_SECONDS>0 时同会话连续收尾只发最后一张（见 feige.debounce_wait）；
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


def has_sink(project: str) -> bool:
    """有地方可发：路由文件对本项目解析出目标，或有默认飞书渠道。"""
    return bool(feige.pick_channel() or feige.resolve_routes(project, feige.load_routes()))


def send_argv(title: str, body: str, stats: dict, debounce_key: str) -> list[str]:
    """拼 `feige.py send` 命令行。一律 --flag=value：值以 "-" 开头时 argparse 会误判。"""
    argv = [sys.executable, str(_REPO_ROOT / "feige.py"), "send",
            f"--title={title}", f"--body={body}", "--status=ok", "--route"]
    for key in ("project", "model", "context", "elapsed"):
        argv.append(f"--{key}={stats.get(key) or ''}")
    for key in ("thinking", "tools"):
        argv.append(f"--{key}={int(stats.get(key) or 0)}")
    if debounce_key:
        argv.append(f"--debounce-key={debounce_key}")
    return argv


def spawn_detached(argv: list[str]) -> None:
    """后台起发送子进程后立刻返回：agent 的 hook/notify 绝不等网络。

    std 句柄全部不继承 agent 的管道（否则 agent 会等管道 EOF，等于没 detach），
    stderr 落 feige 日志文件，发送失败仍可追查。
    """
    import subprocess

    logfh = feige.open_log_file()
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": logfh or subprocess.DEVNULL, "close_fds": True}
    try:
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:  # 尽量脱离 agent 的 job object，agent 退出时不连带杀掉发送进程
                proc = subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:  # job 不允许 breakaway
                proc = subprocess.Popen(argv, creationflags=flags, **kw)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **kw)
        log(f"send spawned (pid {proc.pid}), log: {feige.log_file_path()}")
    finally:
        if logfh:
            logfh.close()  # 子进程已持有自己的句柄


def send_card(title: str, body: str, stats: dict, agent: str, session: str = "") -> None:
    """统一发送：dry-run 或无处可发时把载荷 JSON 打 stdout，否则后台发送；失败只落日志。

    走 --route：有路由文件按项目 fanout，没有则退化为默认单渠道。session 用作
    去抖键（FEIGE_DEBOUNCE_SECONDS>0 时，同会话连续收尾只发最后一张）。
    """
    project = str(stats.get("project") or "")
    title = title or f"{project + ' · ' if project else ''}{agent} 任务完成"
    preview = dry_run()
    if not preview and not has_sink(project):
        log("no channel configured (webhook/app creds/routes missing), preview only")
        preview = True
    if preview:
        _, detail = feige.send_routed(title, body, stats=stats, status="ok",
                                      project=project, dry_run=True)
        try:
            print(detail, flush=True)  # dry-run：卡片 JSON 打 stdout
        except (ValueError, OSError):
            pass
        return
    spawn_detached(send_argv(title, body, stats, f"{agent}:{session}" if session else ""))


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
