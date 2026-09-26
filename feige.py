#!/usr/bin/env python3
"""🕊️ feige-fry-cards — agent 无关的结果汇报核心 + CLI（M1：发卡核心）。

任何编码 agent（ZCode、Claude Code、Codex……）会话收尾时，把「标题 + 摘要正文 +
可选统计字段 + 状态」渲染成一张飞书 Card 2.0 卡片，直发群。纯 Python 标准库，
单文件；后续 tail 守护模式直接 import 本文件的渠道与卡片函数复用。

CLI 用法：

    python feige.py send --title 标题 --body "markdown 正文" \
        [--project X] [--model X] [--thinking N] [--tools N] [--context 42%] \
        [--elapsed 2m38s] [--status ok|error|running] \
        [--channel feishu-webhook|feishu-cardkit] [--chat-id oc_xxx] [--dry-run]

渠道默认选择：有 webhook 环境变量走 feishu-webhook，否则有应用凭据走
feishu-cardkit，都没有则明确报错（打码后）。环境变量与 zcode-feishu-card 对齐：

    FEISHU_CARD_WEBHOOK / FEISHU_WEBHOOK_URL   自定义机器人 webhook（webhook 通道）
    FEISHU_APP_ID / FEISHU_APP_SECRET          应用凭据（CardKit 通道）
    FEISHU_BASE_URL                             默认 https://open.feishu.cn
    FEISHU_NOTIFY_CHAT_ID                       默认群（oc_xxx），CardKit 通道必配

设计不变量（继承自家族踩坑）：摘要是战报不是镜像（正文超长截断）；统计面板只在
完成态（ok/error）做单次脚注，running 态不堆指标；fail-open——发卡失败只落日志，
绝不抛炸调用方；密钥只走环境变量，错误信息里 token/secret/webhook 一律打码。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://open.feishu.cn"
STREAMING_ELEMENT_ID = "streaming_content"  # 流式正文元素 id（家族固定口径）
MAX_BODY_CHARS = 24_000                     # 卡片正文体量硬兜底

# 状态 → header 着色（与 hermes-fry-cards 家族一致：流式中蓝、完成绿、中断/错误红）
STATUS_TEMPLATE = {"running": "blue", "ok": "green", "error": "red"}
STATUS_PREFIX = {"running": "🛠️", "ok": "✅", "error": "❌"}


# ── 通用小件 ─────────────────────────────────────────────────────────────────

def _env(*names: str) -> str:
    """取第一个非空环境变量值。"""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def log(msg: str) -> None:
    """只写 stderr 的尽力而为日志（fail-open：绝不让日志把调用方弄炸）。"""
    try:
        print(f"[{time.strftime('%H:%M:%S')}] feige: {msg}", file=sys.stderr, flush=True)
    except (ValueError, OSError):
        pass


def redact(text: str) -> str:
    """错误信息打码：tenant_access_token / app_secret / webhook URL / 应用密钥值。"""
    s = str(text)
    # 形如 token/secret 字段的值一律替换
    s = re.sub(r'(tenant_access_token["\s:=]+)[A-Za-z0-9_\-]{6,}', r"\1***", s)
    s = re.sub(r'(app_secret["\s:=]+)[A-Za-z0-9]{6,}', r"\1***", s)
    # 当前进程里配置过的真实凭据，明文出现即打码
    for secret in (
        _env("FEISHU_APP_SECRET", "LARK_APP_SECRET"),
        _env("FEISHU_CARD_WEBHOOK", "FEISHU_WEBHOOK_URL"),
        _env("FEISHU_APP_ID", "LARK_APP_ID"),
    ):
        if len(secret) >= 6:
            s = s.replace(secret, "***")
    return s


def clip(text: str) -> str:
    """卡片正文超长硬截断——摘要是战报，不是镜像。"""
    text = text or ""
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[:MAX_BODY_CHARS] + "\n\n…（内容过长已截断）"


# ── 统一卡片模型 ─────────────────────────────────────────────────────────────

def stats_footer(stats: dict | None) -> str:
    """拼装统计脚注行：「📦 项目 · 模型 · 💭思考 · 🔧工具 · 上下文 · ⏱️耗时」。

    约定与家族统一面板口径一致；空字段自动省略。
    """
    if not stats:
        return ""
    parts = []
    project = str(stats.get("project") or "").strip()
    if project:
        parts.append(f"📦 {project}")
    model = str(stats.get("model") or "").strip()
    if model:
        parts.append(model.split("/")[-1])  # 只留模型名末段，渠道前缀太吵
    thinking = stats.get("thinking")
    if thinking:
        parts.append(f"💭{thinking}")
    tools = stats.get("tools")
    if tools:
        parts.append(f"🔧{tools}")
    context = str(stats.get("context") or "").strip()
    if context:
        parts.append(context)  # 形如 "42%" 或 "86.5k/200.0k (43%)"，上游已格式化
    elapsed = str(stats.get("elapsed") or "").strip()
    if elapsed:
        parts.append(f"⏱️ {elapsed}")
    return " · ".join(parts)


def build_card(title: str, body: str, stats: dict | None = None,
               status: str = "ok", streaming: bool = False) -> dict:
    """统一卡片模型 → 飞书 Card 2.0 JSON。

    - 标题按状态加前缀（🛠️/✅/❌），header 按状态着色（蓝/绿/红）；
    - 正文 markdown；streaming=True 时正文元素带 STREAMING_ELEMENT_ID 并开流式配置，
      供后续 CardKit update_content 打字机更新；
    - 统计面板只在完成态（ok/error）做 notation 脚注单次呈现，running 态不出现。
    """
    if status not in STATUS_TEMPLATE:
        raise ValueError(f"unknown status: {status!r} (expect running/ok/error)")
    full_title = f"{STATUS_PREFIX[status]} {title.strip() or '通知'}"

    config: dict = {
        "width_mode": "default",
        "update_multi": True,
        "summary": {"content": full_title},
    }
    if streaming:
        config["streaming_mode"] = True
        config["streaming_config"] = {
            "print_frequency_ms": {"default": 15},
            "print_step": {"default": 1},
            "print_strategy": "fast",
        }

    element: dict = {
        "tag": "markdown",
        "content": clip(body.strip()) or "（空内容）",
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": "0px 0px 0px 0px",
    }
    if streaming:
        element["element_id"] = STREAMING_ELEMENT_ID
    elements = [element]

    footer = stats_footer(stats)
    if status != "running" and footer:
        # Card 2.0 没有 note 组件：脚注用 markdown + text_size:notation
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": footer, "text_size": "notation"})

    return {
        "schema": "2.0",
        "config": config,
        "header": {
            "title": {"tag": "plain_text", "content": full_title},
            "template": STATUS_TEMPLATE[status],
        },
        "body": {"elements": elements},
    }


# ── 底层 HTTP ────────────────────────────────────────────────────────────────

def _post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 10) -> dict:
    """POST JSON，返回解析后的响应；HTTP 错误抛带打码 body 的 RuntimeError。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {redact(raw_err[:300])}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {redact(e.reason)}") from None
    try:
        return json.loads(raw)
    except ValueError:
        return {"code": -1, "msg": raw.decode("utf-8", "replace")[:300]}


# ── 渠道一：feishu-webhook（自定义机器人，一次性整卡）─────────────────────────

def webhook_url() -> str:
    return _env("FEISHU_CARD_WEBHOOK", "FEISHU_WEBHOOK_URL")


def send_feishu_webhook(card: dict, webhook: str = "") -> str:
    """对机器人 webhook 一次性 POST interactive 卡片（无流式，适合结果卡）。"""
    url = webhook or webhook_url()
    if not url:
        raise RuntimeError("missing webhook: set FEISHU_CARD_WEBHOOK or FEISHU_WEBHOOK_URL")
    data = _post_json(url, {"msg_type": "interactive", "card": card})
    if isinstance(data, dict) and data.get("code", 0) not in (0, "0", None):
        raise RuntimeError(f"webhook send failed: code={data.get('code')} msg={redact(data.get('msg', ''))}")
    return "sent (webhook)"


# ── 渠道二：feishu-cardkit（应用凭据，CardKit 流式卡完整生命周期）─────────────

_token: dict = {"value": "", "expire_at": 0.0}


def app_base() -> str:
    base = (_env("FEISHU_BASE_URL", "LARK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return base[: -len("/open-apis")] if base.endswith("/open-apis") else base


def app_api() -> str:
    return app_base() + "/open-apis"


def default_chat_id() -> str:
    return _env("FEISHU_NOTIFY_CHAT_ID", "FEISHU_CARD_CHAT_ID")


def get_tenant_token() -> str:
    """tenant_access_token 获取与缓存（提前 60 秒续期）。"""
    app_id = _env("FEISHU_APP_ID", "LARK_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET", "LARK_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("missing credentials: set FEISHU_APP_ID + FEISHU_APP_SECRET")
    now = time.time()
    if _token["value"] and now < _token["expire_at"]:
        return _token["value"]
    data = _post_json(
        f"{app_api()}/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise RuntimeError(
            f"tenant_access_token failed: code={data.get('code')} msg={redact(data.get('msg', ''))}"
        )
    _token["value"] = data["tenant_access_token"]
    _token["expire_at"] = now + int(data.get("expire") or 3600) - 60
    return _token["value"]


def call_app_api(method: str, path: str, body: dict | None = None) -> dict:
    """带 tenant token 调开放平台 API；HTTP 错误按响应体 JSON 折返，不抛。"""
    req = urllib.request.Request(
        app_api() + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {get_tenant_token()}",
                 "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"code": e.code, "msg": redact(raw[:300])}
    except urllib.error.URLError as e:
        return {"code": -1, "msg": redact(f"network error: {e.reason}")}


def create_card(card: dict) -> str:
    """建卡。body 必须是信封 {"type": "card_json", "data": "<卡片JSON字符串>"}，
    传裸卡片对象会报 99992402（家族硬坑）。返回 card_id。"""
    r = call_app_api("POST", "/cardkit/v1/cards",
                     {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)})
    card_id = (r.get("data") or {}).get("card_id", "")
    if r.get("code") != 0 or not card_id:
        raise RuntimeError(f"create card failed: code={r.get('code')} msg={redact(r.get('msg', ''))}")
    return card_id


def update_content(card_id: str, content: str, sequence: int) -> None:
    """流式正文元素内容更新：PUT .../elements/:element_id/content，带单调递增 sequence。"""
    r = call_app_api("PUT", f"/cardkit/v1/cards/{card_id}/elements/{STREAMING_ELEMENT_ID}/content",
                     {"content": clip(content), "sequence": sequence})
    if r.get("code") != 0:
        raise RuntimeError(f"update content failed: code={r.get('code')} msg={redact(r.get('msg', ''))}")


def seal_card(card_id: str, card: dict, sequence: int) -> int:
    """封卡：PUT 整卡（完成态卡片 JSON）→ PATCH settings 关流式（用 PUT 会 404）。

    返回下一个可用 sequence；关流式失败不致命，只落日志。
    """
    r = call_app_api("PUT", f"/cardkit/v1/cards/{card_id}",
                     {"card": {"type": "card_json",
                               "data": json.dumps(card, ensure_ascii=False)},
                      "sequence": sequence})
    if r.get("code") != 0:
        raise RuntimeError(f"seal card failed: code={r.get('code')} msg={redact(r.get('msg', ''))}")
    sequence += 1
    r = call_app_api("PATCH", f"/cardkit/v1/cards/{card_id}/settings",
                     {"settings": json.dumps({"streaming_mode": False}),
                      "sequence": sequence})
    if r.get("code") != 0:
        log(f"close streaming failed (non-fatal): {r.get('code')} {redact(r.get('msg', ''))}")
    return sequence + 1


def send_card(card_id: str, chat_id: str = "") -> str:
    """按引用把卡片发到群：content = {"type": "card", "data": {"card_id": ...}}。"""
    chat = chat_id or default_chat_id()
    if not chat:
        raise RuntimeError("no chat_id: pass --chat-id or set FEISHU_NOTIFY_CHAT_ID (oc_xxx)")
    content = {"type": "card", "data": {"card_id": card_id}}
    r = call_app_api("POST", "/im/v1/messages?receive_id_type=chat_id",
                     {"receive_id": chat, "msg_type": "interactive",
                      "content": json.dumps(content, ensure_ascii=False)})
    if r.get("code") != 0:
        raise RuntimeError(f"send card failed: code={r.get('code')} msg={redact(r.get('msg', ''))}")
    mid = (r.get("data") or {}).get("message_id", "")
    return f"sent (message_id: {mid})" if mid else "sent"


def send_feishu_cardkit(title: str, body: str, stats: dict | None = None,
                        status: str = "ok", chat_id: str = "",
                        card: dict | None = None) -> str:
    """M1 发卡顺序：建卡（流式）→ 正文更新 → 封卡 → 按引用发到群。

    card 参数可注入预建好的流式卡（供后续 tail 模式复用）；缺省走 build_card。
    """
    streaming_card = card or build_card(title, body, stats=None,
                                        status="running", streaming=True)
    card_id = create_card(streaming_card)
    seq = 1
    try:
        update_content(card_id, body, seq)
        seq += 1
    except RuntimeError as exc:
        log(f"content update skipped (non-fatal): {exc}")
    if status != "running":
        final_card = build_card(title, body, stats=stats, status=status)
        seq = seal_card(card_id, final_card, seq)
    return send_card(card_id, chat_id)


# ── 统一入口 ─────────────────────────────────────────────────────────────────

CHANNELS = ("feishu-webhook", "feishu-cardkit")


def pick_channel() -> str:
    """渠道默认选择：webhook 环境变量优先，其次应用凭据，都没有返回空串。"""
    if webhook_url():
        return "feishu-webhook"
    if _env("FEISHU_APP_ID", "LARK_APP_ID") and _env("FEISHU_APP_SECRET", "LARK_APP_SECRET"):
        return "feishu-cardkit"
    return ""


def send_report(title: str, body: str, stats: dict | None = None, status: str = "ok",
                channel: str = "", chat_id: str = "", dry_run: bool = False) -> tuple[bool, str]:
    """统一发送入口，fail-open：任何异常只落日志，返回 (是否成功, 打码后的说明)。

    dry_run=True 只返回将发送的卡片 JSON 字符串，不发网络请求。
    """
    channel = channel or pick_channel()
    if channel and channel not in CHANNELS:
        return False, f"unknown channel: {channel} (expect {'/'.join(CHANNELS)})"
    if not channel:
        if not dry_run:
            return False, (
                "no channel configured: set FEISHU_CARD_WEBHOOK / FEISHU_WEBHOOK_URL (webhook), "
                "or FEISHU_APP_ID + FEISHU_APP_SECRET (cardkit); "
                f"current: webhook={bool(webhook_url())} app_id={bool(_env('FEISHU_APP_ID','LARK_APP_ID'))}"
            )
        channel = "-"  # dry-run 无渠道配置：只展开卡片 JSON
    try:
        streaming = (channel == "feishu-cardkit")
        card = build_card(title, body, stats=stats if status != "running" else None,
                          status=status, streaming=streaming)
        if dry_run:
            payload = {"msg_type": "interactive", "card": card} if channel == "feishu-webhook" \
                else {"channel": channel, "lifecycle": "create → update_content → seal → send_card",
                      "card": card}
            return True, json.dumps(payload, ensure_ascii=False, indent=2)
        if channel == "feishu-webhook":
            return True, send_feishu_webhook(card)
        return True, send_feishu_cardkit(title, body, stats=stats, status=status, chat_id=chat_id)
    except Exception as exc:  # fail-open：发卡失败只落日志，绝不抛炸调用方
        detail = redact(exc)
        log(f"send failed ({channel}): {detail}")
        return False, detail


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feige",
        description="🕊️ feige-fry-cards：agent 无关的结果汇报——摘要卡片直发飞书群",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send", help="发送一张结果卡片")
    send.add_argument("--title", required=True, help="卡片标题")
    send.add_argument("--body", required=True, help="卡片正文（markdown，战报摘要非全文）")
    send.add_argument("--project", default="", help="项目标签（📦）")
    send.add_argument("--model", default="", help="模型名")
    send.add_argument("--thinking", type=int, default=0, help="思考轮数（💭）")
    send.add_argument("--tools", type=int, default=0, help="工具调用数（🔧）")
    send.add_argument("--context", default="", help="上下文水位，如 42%% 或 86.5k/200.0k (43%%)")
    send.add_argument("--elapsed", default="", help="耗时，如 2m38s（⏱️）")
    send.add_argument("--status", choices=sorted(STATUS_TEMPLATE), default="ok",
                      help="卡片状态：running 蓝 / ok 绿 / error 红（默认 ok）")
    send.add_argument("--channel", choices=CHANNELS, default="",
                      help="发送渠道（默认：有 webhook 走 webhook，否则有应用凭据走 cardkit）")
    send.add_argument("--chat-id", default="", help="目标群 oc_xxx（仅 cardkit；默认 FEISHU_NOTIFY_CHAT_ID）")
    send.add_argument("--dry-run", action="store_true",
                      help="只打印将发送的卡片 JSON，不发网络请求")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:  # Windows 终端 emoji/中文输出兜底
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.command == "send":
        stats = {
            "project": args.project, "model": args.model,
            "thinking": args.thinking, "tools": args.tools,
            "context": args.context, "elapsed": args.elapsed,
        }
        ok, detail = send_report(args.title, args.body, stats=stats, status=args.status,
                                 channel=args.channel, chat_id=args.chat_id,
                                 dry_run=args.dry_run)
        print(detail if args.dry_run else (f"✅ {detail}" if ok else f"❌ {detail}"))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
