#!/usr/bin/env python3
"""🕊️ feige-fry-cards 交互式卡片会话映射、飞书长连接回调与无头续跑引擎。

设计原则：
- 纯 Python 标准库支撑本地会话映射、回调裁决与子进程无头续跑（全离线可测）；
- 飞书长连接（WSClient）按需惰性引入 `lark-oapi`；未安装时优雅降级，不影响
  feige.py / webui.py / Stop hook 其余所有功能；
- 敏感路径（cwd）与原始 session_id 仅保存在本机 `~/.feige-sessions.json`，
  卡片公开字段只放随机不透明令牌 `ctx_id`；
- 回调在 3 秒内同步返回 Toast + 状态卡更新，续跑异步在后台线程执行后回写原卡
  （原卡更新失败时自动退化为回复新卡）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import feige

SESSION_TTL_SECONDS = 7 * 86400
MAX_STORED_SESSIONS = 500
_SESSIONS_LOCK = threading.RLock()

HOST_ALIASES: dict[str, str] = {
    "claude": "claude-code",
    "claude code": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "codex cli": "codex",
    "codex-cli": "codex",
    "zcode": "zcode",
    "kimi": "kimi-code",
    "kimi-code": "kimi-code",
    "kimi_code": "kimi-code",
    "antigravity": "antigravity",
    "agy": "antigravity",
}

HOST_CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude-code": {
        "label": "Claude Code",
        "cli": "claude",
        "resumable": True,
        "note": "支持 claude -p --resume 无头续跑",
    },
    "codex": {
        "label": "Codex CLI",
        "cli": "codex",
        "resumable": True,
        "note": "支持 codex exec resume 无头续跑",
    },
    "zcode": {
        "label": "ZCode",
        "cli": "zcode",
        "resumable": True,
        "note": "支持 zcode -p --resume 无头续跑",
    },
    "kimi-code": {
        "label": "kimi-code",
        "cli": "kimi",
        "resumable": True,
        "note": "支持 kimi -p -S 无头续跑",
    },
    "antigravity": {
        "label": "Antigravity",
        "cli": "agy",
        "resumable": False,
        "note": "Antigravity 暂不支持远程无头续跑，请在本地终端 / IDE 中继续",
    },
}

QUICK_PROMPTS: dict[str, str] = {
    "continue": "请继续完成刚才未完成的任务，并在完成后简洁汇报关键修改与验证结果。",
    "run_tests": "请运行项目相关自动化测试，并汇报测试通过/失败数量及关键结果。",
}


def normalize_host(host: str) -> str:
    s = (host or "").strip().lower()
    return HOST_ALIASES.get(s, s)


def host_label(host: str) -> str:
    nh = normalize_host(host)
    if nh in HOST_CAPABILITIES:
        return str(HOST_CAPABILITIES[nh]["label"])
    return (host or "").strip() or "未知宿主"


def is_host_resumable(host: str, session_id: str | None = None) -> bool:
    nh = normalize_host(host)
    cap = HOST_CAPABILITIES.get(nh)
    if not cap or not cap.get("resumable"):
        return False
    if session_id is not None:
        return bool(session_id.strip())
    return True


def host_supports_resume(host: str, session_id: str = "") -> tuple[bool, str]:
    """严格判定当前记录是否可执行无头续跑：(支持与否, 提示原因)。"""
    nh = normalize_host(host)
    cap = HOST_CAPABILITIES.get(nh)
    if not cap:
        return False, f"当前宿主（{host or '未知'}）暂不支持无头续跑，请在本地终端继续。"
    if not cap.get("resumable"):
        return False, str(cap.get("note") or f"{cap['label']} 暂不支持无头续跑。")
    if not (session_id or "").strip():
        return False, f"{host_label(nh)} 缺少会话 ID，无法定位续跑上下文。"
    return True, ""


def lark_oapi_available() -> bool:
    return importlib.util.find_spec("lark_oapi") is not None


def _no_window_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def resolve_cli_executable(cli_name: str) -> str | None:
    """跨平台查找可执行命令（Windows 兼容 .exe/.cmd/.bat/.ps1）。"""
    if not cli_name:
        return None
    hit = shutil.which(cli_name)
    if hit:
        return hit
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            hit = shutil.which(cli_name + ext)
            if hit:
                return hit
    return None


def probe_host_clis() -> dict[str, dict[str, Any]]:
    """探测各宿主 CLI 在本机的可用状态（只读，供 Studio 状态页展示）。"""
    out: dict[str, dict[str, Any]] = {}
    for key, cap in HOST_CAPABILITIES.items():
        cli = str(cap["cli"])
        path = resolve_cli_executable(cli) if cap["resumable"] else None
        out[key] = {
            "label": cap["label"],
            "cli": cli,
            "resumable": bool(cap["resumable"]),
            "available": bool(path) if cap["resumable"] else False,
            "note": cap["note"],
        }
    return out


# ── 本地安全会话上下文映射（~/.feige-sessions.json）───────────────────────────

def _read_sessions_unlocked(path: Path) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    raw = doc.get("sessions")
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    for cid, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        exp = float(rec.get("expires_at") or 0)
        if exp and exp < now:
            continue
        out[str(cid)] = rec
    return out


def _write_sessions_unlocked(path: Path, sessions: dict[str, dict[str, Any]]) -> None:
    if len(sessions) > MAX_STORED_SESSIONS:
        ordered = sorted(sessions.items(), key=lambda kv: float(kv[1].get("updated_at") or 0), reverse=True)
        sessions = dict(ordered[:MAX_STORED_SESSIONS])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"sessions": sessions}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def register_session(
    *,
    host: str,
    session_id: str,
    cwd: str = "",
    project: str = "",
    model: str = "",
    title: str = "",
    chat_id: str = "",
    card_id: str = "",
    message_id: str = "",
    card_sequence: int = 1,
    last_body: str = "",
    detail_body: str = "",
    tool_summary: str = "",
) -> str:
    """登记本地会话上下文并返回不透明令牌 ctx_id（敏感路径绝不上卡）。"""
    ctx_id = secrets.token_hex(8)
    now = time.time()
    nh = normalize_host(host)
    record: dict[str, Any] = {
        "ctx_id": ctx_id,
        "host": nh,
        "session_id": (session_id or "").strip(),
        "cwd": (cwd or "").strip(),
        "project": (project or "").strip(),
        "model": (model or "").strip(),
        "title": (title or "").strip(),
        "chat_id": (chat_id or "").strip(),
        "card_id": (card_id or "").strip(),
        "message_id": (message_id or "").strip(),
        "card_sequence": max(1, int(card_sequence or 1)),
        "status": "idle",
        "tool_summary": (tool_summary or "").strip(),
        "history": ([{"role": "assistant", "summary": (last_body or "")[:500], "ts": now}] if last_body else []),
        "last_detail": (detail_body or "")[:2000],
        "created_at": now,
        "updated_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
    }
    path = Path(feige.sessions_file_path())
    with _SESSIONS_LOCK:
        sessions = _read_sessions_unlocked(path)
        sessions[ctx_id] = record
        _write_sessions_unlocked(path, sessions)
    return ctx_id


def get_session(ctx_id: str) -> dict[str, Any] | None:
    if not ctx_id:
        return None
    path = Path(feige.sessions_file_path())
    with _SESSIONS_LOCK:
        sessions = _read_sessions_unlocked(path)
        rec = sessions.get(str(ctx_id))
        return dict(rec) if rec else None


def update_session(ctx_id: str, **fields: Any) -> dict[str, Any] | None:
    if not ctx_id:
        return None
    path = Path(feige.sessions_file_path())
    with _SESSIONS_LOCK:
        sessions = _read_sessions_unlocked(path)
        rec = sessions.get(str(ctx_id))
        if rec is None:
            return None
        rec.update(fields)
        rec["updated_at"] = time.time()
        _write_sessions_unlocked(path, sessions)
        return dict(rec)


def list_sessions(limit: int = 30) -> list[dict[str, Any]]:
    """只返回非敏感摘要（隐藏 cwd 与完整 session_id，供 Studio 状态页只读展示）。"""
    path = Path(feige.sessions_file_path())
    with _SESSIONS_LOCK:
        sessions = _read_sessions_unlocked(path)
    ordered = sorted(sessions.values(), key=lambda r: float(r.get("updated_at") or 0), reverse=True)
    out = []
    for rec in ordered[:limit]:
        sid = str(rec.get("session_id") or "")
        out.append({
            "ctx_id": rec.get("ctx_id", ""),
            "host": rec.get("host", ""),
            "project": rec.get("project", ""),
            "status": rec.get("status", "idle"),
            "resumable": host_supports_resume(str(rec.get("host") or ""), sid)[0],
            "has_card": bool(rec.get("card_id")),
            "updated_at": float(rec.get("updated_at") or 0),
        })
    return out


# ── 操作人提取与权限校验 ──────────────────────────────────────────────────────

def extract_operator_id(operator: dict[str, Any] | None) -> str:
    """提取操作人 ID：open_id 优先，缺失回落 user_id（对齐 claw-fry-cards Schema 2）。"""
    if not isinstance(operator, dict):
        return ""
    return str(operator.get("open_id") or operator.get("user_id") or "").strip()


def is_operator_authorized(operator_id: str) -> bool:
    """操作人白名单校验：配了白名单则严格匹配（fail-closed）；未配白名单时允许。"""
    allow = feige.allow_users_list()
    if not allow:
        return True
    if not operator_id:
        return False
    return operator_id in allow


# ── 无头续跑执行器 ────────────────────────────────────────────────────────────

def _wrap_windows_ps1(exe: str, argv: list[str]) -> list[str]:
    if os.name == "nt" and exe.lower().endswith(".ps1"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, *argv]
    return [exe, *argv]


def _claude_permission_mode() -> str:
    raw = (feige._env("FEIGE_CLAUDE_PERMISSION_MODE") or "acceptEdits").strip()
    allowed = {"acceptEdits", "bypassPermissions", "plan", "default", "dontAsk", "auto"}
    return raw if raw in allowed else "acceptEdits"


def build_resume_command(host: str, session_id: str, prompt: str, *,
                         cwd: str = "", codex_out_file: str = "") -> tuple[list[str], str | None]:
    """根据宿主构造无头续跑命令；返回 (argv, cli_error)。"""
    nh = normalize_host(host)
    cap = HOST_CAPABILITIES.get(nh)
    if not cap or not cap.get("resumable"):
        return [], f"{host_label(nh)} 暂不支持无头续跑"
    exe = resolve_cli_executable(str(cap["cli"]))
    if not exe:
        return [], f"本机未找到 {cap['cli']} 命令，无法无头续跑"

    if nh == "claude-code":
        args = [
            "-p", prompt,
            "--resume", session_id,
            "--output-format", "json",
            "--permission-mode", _claude_permission_mode(),
        ]
        return _wrap_windows_ps1(exe, args), None

    if nh == "codex":
        args = ["exec", "resume", session_id, prompt, "--skip-git-repo-check"]
        if codex_out_file:
            args += ["-o", codex_out_file]
        return _wrap_windows_ps1(exe, args), None

    if nh == "zcode":
        args = ["-p", prompt, "--resume", session_id]
        if cwd and Path(cwd).is_dir():
            args += ["--cwd", cwd]
        return _wrap_windows_ps1(exe, args), None

    if nh == "kimi-code":
        args = ["-p", prompt, "-S", session_id]
        return _wrap_windows_ps1(exe, args), None

    return [], f"未知宿主 {nh}"


def _parse_claude_json(stdout: str) -> dict[str, Any] | None:
    s = (stdout or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict) and ("result" in data or "is_error" in data):
                    return data
            except ValueError:
                continue
    return None


def _fmt_ms(ms: float | int | None) -> str:
    if not ms:
        return ""
    try:
        sec = float(ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m {int(sec % 60)}s"


def run_headless_resume(
    record: dict[str, Any],
    prompt: str,
    *,
    timeout: float | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行一次宿主无头续跑，返回标准化结果 dict（fail-open，不抛异常）。"""
    host = normalize_host(str(record.get("host") or ""))
    session_id = str(record.get("session_id") or "").strip()
    cwd = str(record.get("cwd") or "").strip()
    ok_host, reason = host_supports_resume(host, session_id)
    if not ok_host:
        return {"ok": False, "text": "", "error": reason, "elapsed": "", "tokens": None}

    if cwd and not Path(cwd).is_dir():
        return {"ok": False, "text": "", "error": "原会话工作目录已不存在，无法安全续跑", "elapsed": "", "tokens": None}

    codex_out = ""
    tmp_dir_obj = None
    if host == "codex":
        tmp_dir_obj = tempfile.TemporaryDirectory(prefix="feige-codex-")
        codex_out = str(Path(tmp_dir_obj.name) / "last_msg.txt")

    try:
        argv, cli_err = build_resume_command(host, session_id, prompt, cwd=cwd, codex_out_file=codex_out)
        if cli_err:
            return {"ok": False, "text": "", "error": cli_err, "elapsed": "", "tokens": None}

        env = dict(os.environ)
        env["FEIGE_RESUME_CHILD"] = "1"
        env["FEIGE_HOOK_NOTIFY"] = "0"
        env["PYTHONIOENCODING"] = "utf-8"

        eff_timeout = timeout if timeout is not None else feige.resume_timeout_seconds()
        run_fn = runner or subprocess.run
        t0 = time.monotonic()
        proc = run_fn(
            argv,
            cwd=cwd or None,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=eff_timeout,
            creationflags=_no_window_flags() if os.name == "nt" and runner is None else 0,
        )
        elapsed_str = _fmt_ms((time.monotonic() - t0) * 1000.0)
        stdout = str(getattr(proc, "stdout", "") or "").strip()
        stderr = str(getattr(proc, "stderr", "") or "").strip()
        rc = int(getattr(proc, "returncode", 0) or 0)

        tokens = None
        new_sid = session_id
        text = stdout

        if host == "claude-code":
            parsed = _parse_claude_json(stdout)
            if parsed:
                text = str(parsed.get("result") or "").strip() or text
                new_sid = str(parsed.get("session_id") or session_id)
                if parsed.get("duration_ms"):
                    elapsed_str = _fmt_ms(parsed.get("duration_ms"))
                usage = parsed.get("usage") or {}
                if isinstance(usage, dict) and usage.get("output_tokens"):
                    try:
                        tokens = int(usage["output_tokens"])
                    except (TypeError, ValueError):
                        pass
                if parsed.get("is_error") and rc == 0:
                    rc = 1
        elif host == "codex" and codex_out and Path(codex_out).is_file():
            file_msg = Path(codex_out).read_text(encoding="utf-8", errors="replace").strip()
            if file_msg:
                text = file_msg

        if rc != 0:
            err_detail = feige.redact((stderr or text or f"exit code {rc}")[:800])
            return {
                "ok": False,
                "text": text,
                "error": f"{host_label(host)} 续跑失败（rc={rc}）：{err_detail}",
                "elapsed": elapsed_str,
                "tokens": tokens,
                "session_id": new_sid,
            }

        return {
            "ok": True,
            "text": text or "（已完成，无文本输出）",
            "error": "",
            "elapsed": elapsed_str,
            "tokens": tokens,
            "session_id": new_sid,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "error": f"{host_label(host)} 续跑超时", "elapsed": "", "tokens": None}
    except Exception as exc:
        return {"ok": False, "text": "", "error": f"续跑异常：{feige.redact(exc)}", "elapsed": "", "tokens": None}
    finally:
        if tmp_dir_obj is not None:
            try:
                tmp_dir_obj.cleanup()
            except OSError:
                pass


# ── 回调卡片构建与异步回写 ────────────────────────────────────────────────────

def build_resuming_card(record: dict[str, Any], prompt: str) -> dict[str, Any]:
    """点击回调后 3 秒内返回的即时中间态卡片（蓝色处理中，锁定防重复提交）。"""
    title = str(record.get("title") or "").strip() or f"{record.get('project') or host_label(str(record.get('host') or ''))} · 续跑中"
    full_title = f"🛠️ {title}（续跑处理中）"
    hlabel = host_label(str(record.get("host") or ""))
    shown_prompt = feige.clip(prompt.strip(), 500)
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**指令已提交**：\n> {shown_prompt}\n\n⏳ 正在调用 **{hlabel}** 无头续跑，完成后将自动回写本卡片…",
            "text_size": "normal_v2",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"宿主：{hlabel} · 上下文：`#{str(record.get('ctx_id') or '')[:8]}`",
            "text_size": "notation",
        },
    ]
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True, "locales": ["zh_cn", "en_us"],
                   "summary": {"content": full_title}},
        "header": {"title": {"tag": "plain_text", "content": full_title}, "template": "blue"},
        "body": {"elements": elements},
    }


def _build_resumed_result_card(record: dict[str, Any], prompt: str, res: dict[str, Any],
                               logo_key: str = "") -> dict[str, Any]:
    ctx_id = str(record.get("ctx_id") or "")
    host = str(record.get("host") or "")
    project = str(record.get("project") or "")
    model = str(record.get("model") or "")
    base_title = str(record.get("title") or "").strip() or f"{project + ' · ' if project else ''}{host_label(host)} 续跑结果"
    ok = bool(res.get("ok"))
    status = "ok" if ok else "error"
    raw_text = str(res.get("text") or "") if ok else str(res.get("error") or "续跑失败")
    detail = f"**追问指令**：\n> {prompt.strip()}\n\n**结果详情**：\n{raw_text}"
    stats = {
        "project": project,
        "model": model,
        "tokens": res.get("tokens"),
        "elapsed": res.get("elapsed") or "",
    }
    return feige.build_card(
        base_title,
        raw_text,
        stats=stats,
        status=status,
        allow_summarize=True,
        detail_body=detail,
        tool_summary=str(record.get("tool_summary") or ""),
        host=host,
        ctx_id=ctx_id,
        interactive=feige.interactive_enabled(),
        resumable=True,
        logo_image_key=logo_key,
    )


def _execute_resume_and_update(ctx_id: str, prompt: str, *, runner: Callable[..., Any] | None = None) -> None:
    try:
        record = get_session(ctx_id)
        if not record:
            return
        res = run_headless_resume(record, prompt, runner=runner)
        new_sid = str(res.get("session_id") or record.get("session_id") or "")
        hist = list(record.get("history") or [])
        hist.append({"role": "user", "summary": prompt[:200], "ts": time.time()})
        hist.append({"role": "assistant", "summary": (res.get("text") or res.get("error") or "")[:400], "ts": time.time()})
        record = update_session(
            ctx_id,
            status="idle",
            session_id=new_sid,
            history=hist[-10:],
        ) or record

        logo_key = feige.resolve_vendor_logo_image_key(str(record.get("model") or ""))
        new_card = _build_resumed_result_card(record, prompt, res, logo_key=logo_key)

        card_id = str(record.get("card_id") or "").strip()
        seq = max(1, int(record.get("card_sequence") or 1))
        updated = False
        if card_id:
            try:
                next_seq = feige.update_card(card_id, new_card, seq)
                update_session(ctx_id, card_sequence=next_seq)
                updated = True
            except Exception as exc:
                feige.log(f"cardkit update failed, falling back to reply card: {feige.redact(exc)}")

        if not updated:
            chat_id = str(record.get("chat_id") or feige.default_chat_id()).strip()
            reply_mid = str(record.get("message_id") or "").strip()
            sent = feige.send_feishu_cardkit_detail(
                str(record.get("title") or "续跑结果"),
                str(res.get("text") or res.get("error") or ""),
                chat_id=chat_id,
                card=new_card,
                reply_to_message_id=reply_mid,
            )
            update_session(
                ctx_id,
                card_id=sent.get("card_id", card_id),
                message_id=sent.get("message_id", reply_mid),
                card_sequence=1,
            )
    except Exception as exc:
        feige.log(f"resume worker failed (fail-open): {feige.redact(exc)}")
        update_session(ctx_id, status="idle")


def _toast(kind: str, content: str) -> dict[str, Any]:
    return {"toast": {"type": kind, "content": content}}


def handle_card_action(
    payload: dict[str, Any],
    *,
    spawn_bg: bool = True,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """处理飞书 card.action.trigger（纯字典协议，3 秒内同步返回 toast / card）。"""
    if not feige.interactive_enabled():
        return _toast("warning", "交互式卡片功能已在设置中关闭")

    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    if not isinstance(event, dict):
        return _toast("error", "无效的卡片回调载荷")

    operator_id = extract_operator_id(event.get("operator"))
    if not is_operator_authorized(operator_id):
        return _toast("error", "⛔ 你没有权限操作此卡片（不在操作人白名单中）")

    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    val = action.get("value") if isinstance(action.get("value"), dict) else {}
    feige_action = str(val.get("feige_action") or "").strip()
    ctx_id = str(val.get("ctx_id") or "").strip()

    if not feige_action:
        # 不是飞鸽的交互动作，静默忽略
        return {}
    if not ctx_id:
        return _toast("warning", "卡片缺少上下文标识，无法处理")

    record = get_session(ctx_id)
    if not record:
        return _toast("warning", "⌛ 该卡片会话上下文已过期或不存在")

    ctx_obj = event.get("context") if isinstance(event.get("context"), dict) else {}
    cb_chat = str(ctx_obj.get("open_chat_id") or event.get("open_chat_id") or "").strip()
    expected_chat = str(record.get("chat_id") or "").strip()
    if cb_chat and expected_chat and cb_chat != expected_chat:
        return _toast("error", "⛔ 回调群聊与原会话不匹配，已拒绝")

    if feige_action == "unsupported":
        _, reason = host_supports_resume(str(record.get("host") or ""), str(record.get("session_id") or ""))
        return _toast("info", reason or "当前宿主暂不支持远程无头续跑")

    host = str(record.get("host") or "")
    session_id = str(record.get("session_id") or "")
    ok_host, reason = host_supports_resume(host, session_id)
    if not ok_host:
        return _toast("warning", reason)

    if record.get("status") == "resuming":
        return _toast("info", "⏳ 上一条指令正在执行中，请稍候完成后再试")

    if feige_action == "followup":
        form_val = action.get("form_value") if isinstance(action.get("form_value"), dict) else {}
        prompt = str(form_val.get("followup_prompt") or action.get("input_value") or "").strip()
        if not prompt:
            return _toast("warning", "请先输入追问或继续指令后再提交")
    elif feige_action in QUICK_PROMPTS:
        prompt = QUICK_PROMPTS[feige_action]
    else:
        return _toast("warning", f"未知交互动作：{feige_action}")

    # 校验 CLI 是否已装（毫秒级），缺失时立刻提示而不锁卡
    _, cli_err = build_resume_command(host, session_id, prompt, cwd=str(record.get("cwd") or ""))
    if cli_err:
        return _toast("error", cli_err)

    update_session(ctx_id, status="resuming")
    resuming_card = build_resuming_card(record, prompt)

    if spawn_bg:
        t = threading.Thread(
            target=_execute_resume_and_update,
            args=(ctx_id, prompt),
            kwargs={"runner": runner},
            daemon=True,
            name=f"feige-resume-{ctx_id[:8]}",
        )
        t.start()

    return {
        "toast": {"type": "info", "content": f"⏳ 已收到指令，正在调用 {host_label(host)} 续跑…"},
        "card": {"type": "raw", "data": resuming_card},
    }


# ── 飞书长连接（WSClient，按需惰性引入 lark-oapi）──────────────────────────────

def _trigger_to_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    ev = getattr(data, "event", None)
    op = getattr(ev, "operator", None)
    ctx = getattr(ev, "context", None)
    act = getattr(ev, "action", None)
    return {
        "event": {
            "operator": {
                "open_id": getattr(op, "open_id", "") or "",
                "user_id": getattr(op, "user_id", "") or "",
            },
            "context": {
                "open_chat_id": getattr(ctx, "open_chat_id", "") or "",
                "open_message_id": getattr(ctx, "open_message_id", "") or "",
            },
            "action": {
                "tag": getattr(act, "tag", "") or "",
                "name": getattr(act, "name", "") or "",
                "value": getattr(act, "value", None) or {},
                "form_value": getattr(act, "form_value", None) or {},
                "input_value": getattr(act, "input_value", "") or "",
            },
        }
    }


def _dict_to_trigger_response(res: dict[str, Any]) -> Any:
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard,
        CallBackToast,
        P2CardActionTriggerResponse,
    )
    resp = P2CardActionTriggerResponse()
    toast_data = res.get("toast")
    if isinstance(toast_data, dict) and toast_data.get("content"):
        t = CallBackToast()
        t.type = str(toast_data.get("type") or "info")
        t.content = str(toast_data["content"])
        resp.toast = t
    card_data = res.get("card")
    if isinstance(card_data, dict) and card_data.get("data"):
        c = CallBackCard()
        c.type = str(card_data.get("type") or "raw")
        c.data = card_data["data"]
        resp.card = c
    return resp


class FeishuWSManager:
    """管理飞书长连接（WSClient）后台线程，协同 webui.py 保活进程运行。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: Any = None
        self._stop_flag = threading.Event()
        self._state = "stopped"
        self._error = ""
        self._started_at: float | None = None
        self._events_handled = 0
        self._config_key: tuple[str, str, str] | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            app_id = feige._env("FEISHU_APP_ID", "LARK_APP_ID")
            app_secret = feige._env("FEISHU_APP_SECRET", "LARK_APP_SECRET")
            return {
                "available": lark_oapi_available(),
                "enabled": feige.ws_enabled(),
                "credentials_ok": bool(app_id and app_secret),
                "running": bool(self._thread and self._thread.is_alive() and not self._stop_flag.is_set()),
                "state": self._state,
                "error": self._error,
                "started_at": self._started_at,
                "events_handled": self._events_handled,
            }

    def sync_with_config(self) -> dict[str, Any]:
        """根据当前配置决定启动、重启或停止长连接监听（安全幂等，永不抛异常）。"""
        with self._lock:
            enabled = feige.ws_enabled()
            app_id = feige._env("FEISHU_APP_ID", "LARK_APP_ID")
            app_secret = feige._env("FEISHU_APP_SECRET", "LARK_APP_SECRET")
            domain = feige.app_base()
            if not enabled:
                self.stop()
                self._state = "stopped"
                self._error = ""
                return self.status()
            if not lark_oapi_available():
                self.stop()
                self._state = "unavailable"
                self._error = "未安装 lark-oapi（仅影响长连接回调监听；安装：pip install lark-oapi）"
                return self.status()
            if not app_id or not app_secret:
                self.stop()
                self._state = "stopped"
                self._error = "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET"
                return self.status()
            key = (app_id, app_secret, domain)
            if self._thread and self._thread.is_alive() and self._config_key == key and not self._stop_flag.is_set():
                return self.status()
            self.stop()
            self._config_key = key
            self._stop_flag.clear()
            self._state = "connecting"
            self._error = ""
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._run_thread,
                args=(app_id, app_secret, domain),
                daemon=True,
                name="feige-feishu-ws",
            )
            self._thread.start()
            return self.status()

    def stop(self) -> None:
        with self._lock:
            self._stop_flag.set()
            loop = self._loop
            client = self._ws_client
            if loop is not None and not loop.is_closed():
                def _shutdown() -> None:
                    try:
                        if client is not None and hasattr(client, "_disconnect"):
                            loop.create_task(client._disconnect())
                    except Exception:
                        pass
                    for t in asyncio.all_tasks(loop):
                        t.cancel()
                try:
                    loop.call_soon_threadsafe(_shutdown)
                except RuntimeError:
                    pass
            th = self._thread
            self._thread = None
            self._ws_client = None
            self._loop = None
            self._config_key = None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2.0)

    def _on_trigger(self, data: Any) -> Any:
        try:
            payload = _trigger_to_dict(data)
            res = handle_card_action(payload)
            with self._lock:
                self._events_handled += 1
            return _dict_to_trigger_response(res)
        except Exception as exc:
            feige.log(f"ws card callback error: {feige.redact(exc)}")
            return _dict_to_trigger_response(_toast("error", "卡片回调处理异常，请查看 feige 日志"))

    def _run_thread(self, app_id: str, app_secret: str, domain: str) -> None:
        while not self._stop_flag.is_set():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                import lark_oapi as lark
                from lark_oapi.ws import client as wsc

                wsc.loop = loop
                handler = (
                    lark.EventDispatcherHandler.builder("", "")
                    .register_p2_card_action_trigger(self._on_trigger)
                    .build()
                )
                ws = lark.ws.Client(
                    app_id,
                    app_secret,
                    event_handler=handler,
                    domain=domain,
                    log_level=lark.LogLevel.ERROR,
                    auto_reconnect=True,
                )

                def _mark_connected() -> None:
                    with self._lock:
                        self._state = "running"
                        self._error = ""

                def _mark_reconnecting() -> None:
                    with self._lock:
                        self._state = "connecting"

                ws.on_reconnected = _mark_connected
                ws.on_reconnecting = _mark_reconnecting
                with self._lock:
                    self._loop = loop
                    self._ws_client = ws
                    self._state = "running"
                    self._error = ""
                ws.start()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                err = feige.redact(exc)
                with self._lock:
                    self._state = "error"
                    self._error = err
                feige.log(f"ws listener error: {err}")
            finally:
                with self._lock:
                    if self._loop is loop:
                        self._loop = None
                    self._ws_client = None
                try:
                    loop.close()
                except Exception:
                    pass
            if self._stop_flag.is_set():
                break
            self._stop_flag.wait(5.0)
        with self._lock:
            if self._stop_flag.is_set():
                self._state = "stopped"


_WS_MANAGER: FeishuWSManager | None = None
_WS_MANAGER_LOCK = threading.Lock()


def get_ws_manager() -> FeishuWSManager:
    global _WS_MANAGER
    with _WS_MANAGER_LOCK:
        if _WS_MANAGER is None:
            _WS_MANAGER = FeishuWSManager()
        return _WS_MANAGER


def run_ws_listener_forever() -> int:
    """前台阻塞运行飞书长连接监听（供 `python feige.py listen` 使用）。"""
    if not lark_oapi_available():
        feige.log("lark-oapi not installed; install with: pip install lark-oapi")
        return 1
    app_id = feige._env("FEISHU_APP_ID", "LARK_APP_ID")
    app_secret = feige._env("FEISHU_APP_SECRET", "LARK_APP_SECRET")
    if not app_id or not app_secret:
        feige.log("missing FEISHU_APP_ID / FEISHU_APP_SECRET")
        return 1
    mgr = get_ws_manager()
    # 独立 listen 命令默认强制启用
    os.environ["FEIGE_WS_ENABLED"] = "1"
    st = mgr.sync_with_config()
    print(f"🕊️ feige 飞书长连接监听已启动（state={st['state']}），Ctrl+C 退出", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        mgr.stop()
        print("🕊️ feige 长连接监听已停止", flush=True)
    return 0
