"""🕊️ feige-fry-cards 适配器公共件：开关、项目标签、teaser、fail-open 发送。

约定（与 M2 ZCode hook 一致）：
- FEIGE_HOOK_NOTIFY=1 才启用，默认关闭；已在路由文件里配了群的项目**自动开启**
  （per-project 开关，见 routes_auto_enabled）；
- FEIGE_DRY_RUN=1 把卡片 JSON 打到 stderr，不发网络；
- 插件 userConfig（CLAUDE_PLUGIN_OPTION_* / ZCODE_USER_CONFIG_*）映射为 feige 环境变量；
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


# 插件 userConfig → feige 环境变量。Claude Code 注入为 CLAUDE_PLUGIN_OPTION_<KEY>，
# ZCode 注入为 ZCODE_USER_CONFIG_<KEY>；键名与 .claude-plugin / .zcode-plugin 清单一致。
PLUGIN_OPTION_PREFIXES = ("CLAUDE_PLUGIN_OPTION_", "ZCODE_USER_CONFIG_")
PLUGIN_OPTION_ENV = {
    "HOOK_NOTIFY": "FEIGE_HOOK_NOTIFY",
    "WEBHOOK_URL": "FEISHU_CARD_WEBHOOK",
    "WEBHOOK_SECRET": "FEISHU_WEBHOOK_SECRET",
    "APP_ID": "FEISHU_APP_ID",
    "APP_SECRET": "FEISHU_APP_SECRET",
    "BASE_URL": "FEISHU_BASE_URL",
    "NOTIFY_CHAT_ID": "FEISHU_NOTIFY_CHAT_ID",
    "DEBOUNCE_SECONDS": "FEIGE_DEBOUNCE_SECONDS",
    "ROUTES_FILE": "FEIGE_ROUTES_FILE",
    "STATS_SCOPE": "FEIGE_STATS_SCOPE",
    "LLM_API_KEY": "FEIGE_LLM_API_KEY",
    "LLM_BASE_URL": "FEIGE_LLM_BASE_URL",
    "LLM_MODEL": "FEIGE_LLM_MODEL",
    "LLM_MAX_CHARS": "FEIGE_LLM_MAX_CHARS",
    "SUMMARIZE": "FEIGE_SUMMARIZE",
}


def apply_plugin_options(env=None) -> None:
    """把插件设置页填的值落到 feige 环境变量上；真实环境变量优先，不覆盖。

    子进程（后台发送）继承 os.environ，所以这里改一次全链路生效。
    """
    env = os.environ if env is None else env
    for key, target in PLUGIN_OPTION_ENV.items():
        if (env.get(target) or "").strip():
            continue
        for prefix in PLUGIN_OPTION_PREFIXES:
            value = (env.get(prefix + key) or "").strip()
            if value:
                env[target] = value
                break


def hook_enabled() -> bool:
    return (os.environ.get("FEIGE_HOOK_NOTIFY") or "").strip().lower() in _ON


def stats_scope() -> str:
    """统计口径：turn（单轮，默认）| session（全程）；解析见 feige.stats_scope。"""
    return feige.stats_scope()


def dry_run() -> bool:
    return (os.environ.get("FEIGE_DRY_RUN") or "").strip().lower() in _ON


def teaser(text: str, limit: int = TEASER_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ……"


# 总结启用时给 feige 的原文上限：够 LLM 浓缩出像样的战报，又不至于把命令行
# 参数撑爆（Windows CreateProcess 命令行上限 ~32k，argv 走 subprocess 列表也受此限）。
FULL_BODY_CHARS = 6_000


def summarize_enabled() -> bool:
    """正文总结是否启用（env/设置文件解析口径见 feige.llm_config）。"""
    return feige.llm_config()["enabled"]


def body_for_card(text: str, empty_note: str = "……（本轮无文本输出）") -> str:
    """卡片正文：总结未启用保持 teaser 300 字老口径；启用时传更长原文，
    由后台发送进程（feige.py send）调 LLM 总结，失败再退化硬截断。"""
    text = (text or "").strip()
    if not text:
        return empty_note
    if not summarize_enabled():
        return teaser(text)
    return text if len(text) <= FULL_BODY_CHARS else text[:FULL_BODY_CHARS].rstrip() + " ……"


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
    """从 cwd 提取项目标签：GitHub Files\\X / workspace\\X 一级目录，否则目录末段。

    这是**路由键**：~/.feige-routes.json 的 routes 键必须与它逐字相同（大小写、
    空格都算），否则命不中、只能落到 "*" 兜底（那就等于「所有项目发同一个群」）。
    """
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


def routes_auto_enabled(project: str) -> bool:
    """该项目的自动战报是否开启：有路由目标就发，没有就不发（per-project 总闸）。

    与 has_sink 的区别：has_sink 是「**能不能**发」（默认渠道或路由），这里是
    「**该不该**发」。用户要求按群自动开关——同一个宿主里 A 项目发、B 项目不发，
    只能由路由决定：配了 routes[project] 即视为自动开启，未配置则不自动发。
    环境变量 FEIGE_HOOK_NOTIFY=0 / 插件设置 hook_notify 关闭仍是全局硬闸，先判。
    """
    return bool(feige.resolve_routes(project, feige.load_routes()))


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
    if stats.get("tokens") is not None and str(stats.get("tokens")).strip():
        argv.append(f"--tokens={stats['tokens']}")
    for key in ("thinking", "tools"):
        if stats.get(key) is not None and str(stats.get(key)).strip():
            argv.append(f"--{key}={int(stats[key])}")
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
    """统一发送：dry-run 或无处可发时把载荷 JSON 打 stderr，否则后台发送；失败只落日志。

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
            # 打 stderr：Stop hook 的 stdout 会被宿主当 hook JSON 输出解析（Claude Code / Codex）
            print(detail, file=sys.stderr, flush=True)
        except (ValueError, OSError):
            pass
        return
    spawn_detached(send_argv(title, body, stats, f"{agent}:{session}" if session else ""))


def run(main, project: str | None = None) -> None:
    """驱动入口：默认关 directly return；任何异常 exit 0。

    project 非空时按「每群自动开关」判定：该项目在路由文件里有目标 → 自动发送
    （即使 FEIGE_HOOK_NOTIFY 未开）；没有目标 → 只有全局开关显式打开时才发。
    全局关闭（FEIGE_HOOK_NOTIFY=0/插件 hook_notify 关）永远是硬闸，per-project
    只在「没配全局值」时生效——否则用户关不掉。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        apply_plugin_options()
    except Exception:
        pass
    if not hook_enabled():
        auto = False
        if project:
            try:
                auto = routes_auto_enabled(project)
            except Exception:
                auto = False
        if not auto:
            return
    try:
        main()
    except Exception:
        log(f"adapter failed (fail-open):\n{traceback.format_exc()}")
