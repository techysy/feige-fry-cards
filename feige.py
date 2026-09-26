#!/usr/bin/env python3
"""🕊️ feige-fry-cards — agent 无关的结果汇报核心 + CLI。

任何编码 agent（ZCode、Claude Code、Codex……）会话收尾时，把「标题 + 摘要正文 +
可选统计字段 + 状态」渲染成结果卡片，直发群。纯 Python 标准库，单文件；
tail 守护 / hook / 适配器直接 import 本文件的渠道与卡片函数复用。

渠道（CHANNELS）：

    feishu-webhook    飞书自定义机器人 webhook，一次性整卡（交互卡）
    feishu-cardkit    飞书应用凭据，CardKit 建卡 → 按引用发群（交互卡；流式生命周期函数留给 tail 模式）
    dingtalk-webhook  钉钉自定义机器人 webhook，markdown 消息
    telegram          Telegram Bot API sendMessage（纯文本版式，不用 parse_mode）

CLI 用法：

    python feige.py send --title 标题 --body "markdown 正文" \
        [--project X] [--model X] [--thinking N] [--tools N] [--context 42%] \
        [--elapsed 2m38s] [--status ok|error|running] \
        [--channel <CHANNELS>] [--chat-id ...] [--webhook ...] [--route] [--dry-run]

渠道默认选择：有飞书 webhook 环境变量走 feishu-webhook，否则有应用凭据走
feishu-cardkit，都没有则明确报错（打码后）；钉钉/Telegram 显式 --channel 或走
--route 路由。--route 时按 --project 解析路由文件（FEIGE_ROUTES_FILE，默认
~/.feige-routes.json）做多目标 fanout，路由不可用时退化为单渠道行为。

环境变量：

    FEISHU_CARD_WEBHOOK / FEISHU_WEBHOOK_URL   飞书机器人 webhook
    FEISHU_WEBHOOK_SECRET                       飞书机器人「签名校验」密钥（可选）
    FEISHU_APP_ID / FEISHU_APP_SECRET          飞书应用凭据（CardKit 通道）
    FEISHU_BASE_URL                             默认 https://open.feishu.cn
    FEISHU_NOTIFY_CHAT_ID                       飞书默认群（oc_xxx），CardKit 必配
    DINGTALK_WEBHOOK                            钉钉机器人 webhook
    DINGTALK_SECRET                             钉钉机器人「加签」密钥（SEC 开头，可选）
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID      Telegram bot token / 目标 chat（群为负数）
    FEIGE_ROUTES_FILE                           群路由文件（默认 ~/.feige-routes.json）
    FEIGE_DEBOUNCE_SECONDS                      收尾去抖秒数（配合 --debounce-key，默认 0 关闭）
    FEIGE_LOG_FILE                              后台发送的日志文件（默认 <临时目录>/feige.log）

设计不变量（继承自家族踩坑）：摘要是战报不是镜像（正文超长截断）；统计面板只在
完成态（ok/error）做单次脚注，running 态不堆指标；fail-open——发卡失败只落日志，
绝不抛炸调用方；密钥只走环境变量，错误信息里 token/secret/webhook 一律打码。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://open.feishu.cn"
STREAMING_ELEMENT_ID = "streaming_content"  # 流式正文元素 id（家族固定口径）
# 渠道体量上限（官方口径 + 余量）。按字符截断不够：中文 UTF-8 占 3 字节，24k 字就是 72KB
BODY_MAX_BYTES = 15_000    # 飞书 webhook 请求体 ≤20KB、钉钉消息 ≤20000 字节，给卡片骨架留余量
TELEGRAM_MAX_UNITS = 4096  # Telegram sendMessage text ≤4096，按 UTF-16 单元计（emoji 占 2）
TRUNC_NOTE = "\n\n…（内容过长已截断）"

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
    """错误信息打码：tenant_access_token / app_secret / webhook URL / bot token 等。"""
    s = str(text)
    # 先按当前进程里配置过的真实凭据整串打码（要在形态打码之前，否则整串对不上）
    for secret in (
        _env("FEISHU_APP_SECRET", "LARK_APP_SECRET"),
        _env("FEISHU_CARD_WEBHOOK", "FEISHU_WEBHOOK_URL"),
        _env("FEISHU_WEBHOOK_SECRET"),
        _env("FEISHU_APP_ID", "LARK_APP_ID"),
        _env("DINGTALK_WEBHOOK"),
        _env("DINGTALK_SECRET"),
        _env("TELEGRAM_BOT_TOKEN"),
    ):
        if len(secret) >= 6:
            s = s.replace(secret, "***")
    # 形如 token/secret 字段的值一律替换
    s = re.sub(r'(tenant_access_token["\s:=]+)[A-Za-z0-9_\-]{6,}', r"\1***", s)
    s = re.sub(r'(app_secret["\s:=]+)[A-Za-z0-9]{6,}', r"\1***", s)
    # Telegram bot token（bot<数字>:<串> 形态）
    s = re.sub(r'bot\d{6,}:[A-Za-z0-9_\-]{10,}', "bot***", s)
    # webhook URL 形态（路由里另配的 webhook 不在环境变量里，只能按形态认）：
    # 飞书/Lark /bot/v2/hook/<id>、钉钉 access_token=<串>、加签 sign=<串>
    s = re.sub(r'(/hook/)[A-Za-z0-9_\-]{6,}', r"\1***", s)
    s = re.sub(r'\b((?:access_token|sign)=)[^&\s"\']+', r"\1***", s)
    return s


def _size(text: str, unit: str) -> int:
    if unit == "utf16":
        return len(text.encode("utf-16-le")) // 2
    return len(text.encode("utf-8"))


def clip(text: str, limit: int = BODY_MAX_BYTES, unit: str = "bytes") -> str:
    """正文超长硬截断到渠道上限（含截断提示）——摘要是战报，不是镜像。

    unit："bytes" 按 UTF-8 字节（飞书/钉钉），"utf16" 按 UTF-16 单元（Telegram）。
    """
    text = text or ""
    if _size(text, unit) <= limit:
        return text
    budget = limit - _size(TRUNC_NOTE, unit)
    lo, hi = 0, len(text)  # 二分找最长可容纳前缀
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(text[:mid], unit) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + TRUNC_NOTE


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


def feishu_webhook_secret(webhook: str = "", secret: str = "") -> str:
    """签名密钥：显式传入优先；环境变量密钥只配环境变量里的 webhook——路由里另指定
    的 webhook 是另一个机器人，拿默认密钥签只会签错。"""
    return secret or ("" if webhook else _env("FEISHU_WEBHOOK_SECRET"))


def feishu_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人「签名校验」：key = "{timestamp}\\n{secret}"，对空串 HMAC-SHA256，base64。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


def send_feishu_webhook(card: dict, webhook: str = "", secret: str = "") -> str:
    """对机器人 webhook 一次性 POST interactive 卡片（无流式，适合结果卡）。

    机器人开了「签名校验」时配 FEISHU_WEBHOOK_SECRET（或路由目标 secret），载荷带
    timestamp + sign。
    """
    url = webhook or webhook_url()
    if not url:
        raise RuntimeError("missing webhook: set FEISHU_CARD_WEBHOOK or FEISHU_WEBHOOK_URL")
    body: dict = {"msg_type": "interactive", "card": card}
    secret = feishu_webhook_secret(webhook, secret)
    if secret:
        ts = int(time.time())
        body.update(timestamp=str(ts), sign=feishu_sign(secret, ts))
    data = _post_json(url, body)
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
    """结果卡：直接建终态卡 → 按引用发到群，共 2 次调用。

    不走流式：卡片是封好之后才发进群的，群里看不到打字机过程，旧的
    「建流式卡 → 更新同样的正文 → 整卡 PUT → PATCH 关流式 → 发群」只是多花 3 次调用；
    running 态的一次性卡也不该开着流式模式没人来关。流式生命周期（create_card /
    update_content / seal_card）保留给 tail 模式：先 send_card 再持续更新才有意义。
    card 参数可注入预建好的卡片，原样建卡发送。
    """
    card_id = create_card(card or build_card(title, body, stats=stats, status=status))
    return send_card(card_id, chat_id)


# ── 渠道三：dingtalk-webhook（钉钉自定义机器人，markdown 消息）────────────────

def render_dingtalk(title: str, body: str, stats: dict | None = None,
                    status: str = "ok") -> dict:
    """统一卡片模型 → 钉钉 markdown 消息载荷。

    钉钉自定义机器人没有交互卡/CardKit，markdown 就是全部版面：
    标题行 + 正文 + --- + 统计脚注一行（脚注仍只在完成态出现）。
    """
    if status not in STATUS_TEMPLATE:
        raise ValueError(f"unknown status: {status!r} (expect running/ok/error)")
    full_title = f"{STATUS_PREFIX[status]} {title.strip() or '通知'}"
    parts = [f"### {full_title}", "", clip(body.strip()) or "（空内容）"]
    footer = stats_footer(stats)
    if status != "running" and footer:
        parts += ["", "---", footer]
    return {"msgtype": "markdown",
            "markdown": {"title": full_title, "text": "\n".join(parts)}}


def dingtalk_secret(webhook: str = "", secret: str = "") -> str:
    """加签密钥：显式传入优先；DINGTALK_SECRET 只配环境变量里的 webhook（同飞书口径）。"""
    return secret or ("" if webhook else _env("DINGTALK_SECRET"))


def dingtalk_signed_url(url: str, secret: str, timestamp_ms: int) -> str:
    """钉钉「加签」：HMAC-SHA256(key=secret, msg="{timestamp}\\n{secret}")，base64 后
    URL 编码，以 &timestamp=&sign= 追加到 webhook。"""
    msg = f"{timestamp_ms}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    return f"{url}{'&' if '?' in url else '?'}timestamp={timestamp_ms}&sign={sign}"


def send_dingtalk_webhook(payload: dict, webhook: str = "", secret: str = "") -> str:
    """POST 钉钉机器人 webhook；errcode 非 0 即失败。

    机器人安全设置选了「加签」时配 DINGTALK_SECRET（SEC 开头，或路由目标 secret）。
    """
    url = webhook or _env("DINGTALK_WEBHOOK")
    if not url:
        raise RuntimeError("missing webhook: set DINGTALK_WEBHOOK or pass --webhook")
    secret = dingtalk_secret(webhook, secret)
    if secret:
        url = dingtalk_signed_url(url, secret, int(time.time() * 1000))
    data = _post_json(url, payload)
    errcode = data.get("errcode", 0) if isinstance(data, dict) else 0
    if errcode not in (0, "0", None):
        raise RuntimeError(f"dingtalk send failed: errcode={errcode} errmsg={redact(data.get('errmsg', ''))}")
    return "sent (dingtalk)"


# ── 渠道四：telegram（Bot API sendMessage，纯文本版式）──────────────────────

def render_telegram_text(title: str, body: str, stats: dict | None = None,
                         status: str = "ok") -> str:
    """统一卡片模型 → Telegram 纯文本（不启 parse_mode，避开 MarkdownV2 转义地雷）。

    标题、正文按原文、统计一行、「———」分隔；脚注仍只在完成态出现。
    """
    if status not in STATUS_TEMPLATE:
        raise ValueError(f"unknown status: {status!r} (expect running/ok/error)")
    head = f"{STATUS_PREFIX[status]} {title.strip() or '通知'}\n\n"
    footer = stats_footer(stats)
    tail = f"\n\n———\n{footer}" if status != "running" and footer else ""
    budget = TELEGRAM_MAX_UNITS - _size(head + tail, "utf16")
    text = head + (clip(body.strip(), max(budget, 0), "utf16") or "（空内容）") + tail
    return clip(text, TELEGRAM_MAX_UNITS, "utf16")  # 标题/脚注本身超长时的最后兜底


def send_telegram(title: str, body: str, stats: dict | None = None, status: str = "ok",
                  chat_id: str = "", token: str = "") -> str:
    """POST https://api.telegram.org/bot<token>/sendMessage（纯文本）。"""
    token = token or _env("TELEGRAM_BOT_TOKEN")
    chat = chat_id or _env("TELEGRAM_CHAT_ID")
    if not token:
        raise RuntimeError("missing token: set TELEGRAM_BOT_TOKEN (BotFather)")
    if not chat:
        raise RuntimeError("missing chat: set TELEGRAM_CHAT_ID or pass --chat-id")
    data = _post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                      {"chat_id": chat,
                       "text": render_telegram_text(title, body, stats, status)})
    if not data.get("ok"):
        raise RuntimeError(f"telegram send failed: {redact(data.get('description', data))}")
    return "sent (telegram)"


# ── 统一入口 ─────────────────────────────────────────────────────────────────

CHANNELS = ("feishu-webhook", "feishu-cardkit", "dingtalk-webhook", "telegram")


def pick_channel() -> str:
    """渠道默认选择：飞书 webhook 优先，其次飞书应用凭据；钉钉/Telegram 不进默认
    选择（显式 --channel 或 --route 路由指定），都没有返回空串。"""
    if webhook_url():
        return "feishu-webhook"
    if _env("FEISHU_APP_ID", "LARK_APP_ID") and _env("FEISHU_APP_SECRET", "LARK_APP_SECRET"):
        return "feishu-cardkit"
    return ""


def send_report(title: str, body: str, stats: dict | None = None, status: str = "ok",
                channel: str = "", chat_id: str = "", webhook: str = "",
                token: str = "", dry_run: bool = False, secret: str = "") -> tuple[bool, str]:
    """统一发送入口，fail-open：任何异常只落日志，返回 (是否成功, 打码后的说明)。

    dry_run=True 只返回将发送的载荷 JSON 字符串，不发网络请求。
    webhook/token/chat_id/secret 为路由目标级别的显式覆盖，缺省读各渠道环境变量
    （secret 是 webhook 的签名密钥，只对 feishu-webhook / dingtalk-webhook 有效）。
    """
    channel = channel or pick_channel()
    if channel and channel not in CHANNELS:
        return False, f"unknown channel: {channel} (expect {'/'.join(CHANNELS)})"
    if not channel:
        if not dry_run:
            return False, (
                "no channel configured: set FEISHU_CARD_WEBHOOK / FEISHU_WEBHOOK_URL (feishu webhook), "
                "FEISHU_APP_ID + FEISHU_APP_SECRET (cardkit), DINGTALK_WEBHOOK or TELEGRAM_BOT_TOKEN; "
                f"current: feishu_webhook={bool(webhook_url())} feishu_app={bool(_env('FEISHU_APP_ID','LARK_APP_ID'))}"
            )
        channel = "-"  # dry-run 无渠道配置：只展开卡片 JSON
    try:
        eff_stats = stats if status != "running" else None
        # 规范化汇报载荷（title/body/stats/status）按渠道各自渲染
        if channel == "dingtalk-webhook":
            payload = render_dingtalk(title, body, eff_stats, status)
            if dry_run:
                return True, json.dumps({"channel": channel, "lifecycle": "one-shot",
                                         "signed": bool(dingtalk_secret(webhook, secret)),
                                         "payload": payload}, ensure_ascii=False, indent=2)
            return True, send_dingtalk_webhook(payload, webhook, secret)
        if channel == "telegram":
            payload = {"chat_id": chat_id or _env("TELEGRAM_CHAT_ID") or "<TELEGRAM_CHAT_ID>",
                       "text": render_telegram_text(title, body, eff_stats, status)}
            if dry_run:
                return True, json.dumps({"channel": channel, "lifecycle": "one-shot",
                                         "payload": payload}, ensure_ascii=False, indent=2)
            return True, send_telegram(title, body, stats=stats, status=status,
                                       chat_id=chat_id, token=token)
        # feishu 两渠道（含 "-" 无渠道 dry-run 预览）：Card 2.0 终态卡
        card = build_card(title, body, stats=eff_stats, status=status)
        if dry_run:
            if channel == "feishu-webhook":
                out = {"msg_type": "interactive", "card": card}
                if feishu_webhook_secret(webhook, secret):
                    out.update(timestamp="<unix 秒>", sign="<HMAC-SHA256 签名>")
            else:
                out = {"channel": channel, "lifecycle": "create_card → send_card", "card": card}
            return True, json.dumps(out, ensure_ascii=False, indent=2)
        if channel == "feishu-webhook":
            return True, send_feishu_webhook(card, webhook, secret)
        return True, send_feishu_cardkit(title, body, chat_id=chat_id, card=card)
    except Exception as exc:  # fail-open：发卡失败只落日志，绝不抛炸调用方
        detail = redact(exc)
        log(f"send failed ({channel}): {detail}")
        return False, detail


# ── 群路由（项目 → 多目标 fanout）───────────────────────────────────────────

def routes_file_path(env: dict | None = None) -> str:
    """路由文件路径：FEIGE_ROUTES_FILE > ~/.feige-routes.json。"""
    env = env if env is not None else os.environ
    override = str(env.get("FEIGE_ROUTES_FILE") or "").strip()
    if override:
        return override
    return str(Path.home() / ".feige-routes.json")


def load_routes(path: str = "") -> dict | None:
    """读路由文件；不存在/损坏/结构不合法返回 None 并落日志（fail-open）。

    文件可含 webhook/chat_id 等半敏感信息（$ENV 引用可规避），错误信息打码。
    """
    path = path or routes_file_path()
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None  # 没配路由是常态，不叫错误
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        log(f"routes file broken ({redact(exc)}), falling back to single-channel")
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("routes"), dict):
        log("routes file missing top-level \"routes\" object, falling back to single-channel")
        return None
    return doc


def resolve_routes(project: str, doc: dict | None, env: dict | None = None) -> list[dict]:
    """纯函数：项目 → 展开后的目标列表。

    匹配：精确项目名 → "*" 通配兜底 → []（调用方退化单渠道）。
    目标项里形如 "$NAME" 的整字符串值替换为 env[NAME]（秘密不进路由文件）。
    引用的变量未设置/为空时，把变量名记入目标的 "_unresolved" 列表——空值绝不能
    交给 send_report，否则会静默回退到默认渠道环境变量，把战报发进别的群。
    """
    env = env if env is not None else os.environ
    routes = (doc or {}).get("routes") or {}
    targets = routes.get(project) or routes.get("*") or []
    if not isinstance(targets, list):
        return []
    out = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        expanded: dict = {}
        unresolved = []
        for key, value in t.items():
            if isinstance(value, str) and value.startswith("$") and len(value) > 1:
                expanded[key] = str(env.get(value[1:], "") or "").strip()
                if not expanded[key]:
                    unresolved.append(value[1:])
            else:
                expanded[key] = value
        if unresolved:
            expanded["_unresolved"] = unresolved
        out.append(expanded)
    return out


def send_routed(title: str, body: str, stats: dict | None = None, status: str = "ok",
                project: str = "", dry_run: bool = False,
                doc: dict | None = None) -> tuple[bool, str]:
    """按路由 fanout：每个目标独立发送（单目标失败不影响其余），返回聚合结果。

    无路由文件 / 文件损坏 / 项目无匹配且无 "*" 兜底：退化为现有单渠道行为。
    """
    doc = load_routes() if doc is None else doc
    targets = resolve_routes(project, doc) if doc else []
    if not targets:
        if doc is not None:
            log(f"no route for project {project or '(none)'!r} (no \"*\" fallback), single-channel")
        return send_report(title, body, stats=stats, status=status, dry_run=dry_run)
    results: list[tuple[bool, str]] = []
    for t in targets:
        channel = str(t.get("channel") or "").strip()
        if not channel:
            # 只报键名：展开后的值可能含路由级密钥，redact 并不认识它们
            log(f"route target missing channel, skipped (keys: {sorted(t)})")
            results.append((False, "[?] route target missing channel"))
            continue
        if t.get("_unresolved"):
            names = ", ".join(f"${n}" for n in t["_unresolved"])
            detail = f"unset env var(s) {names}, target skipped (no fallback to default channel)"
            log(f"route [{channel}] {detail}")
            results.append((False, f"[{channel}] {detail}"))
            continue
        ok, detail = send_report(
            title, body, stats=stats, status=status, channel=channel,
            chat_id=str(t.get("chat_id") or ""), webhook=str(t.get("webhook") or ""),
            token=str(t.get("token") or ""), secret=str(t.get("secret") or ""),
            dry_run=dry_run)
        results.append((ok, f"[{channel}] {detail}"))
    return all(ok for ok, _ in results), "\n".join(d for _, d in results)


# ── 后台发送支撑：日志文件 + 收尾去抖 ──────────────────────────────────────────

LOG_ROTATE_BYTES = 1_000_000


def log_file_path() -> str:
    """后台发送子进程的 stderr 落点：FEIGE_LOG_FILE > <临时目录>/feige.log。"""
    return _env("FEIGE_LOG_FILE") or str(Path(tempfile.gettempdir()) / "feige.log")


def open_log_file():
    """以追加方式打开日志文件（超 1MB 先轮转为 .1）；打不开返回 None（fail-open）。"""
    path = Path(log_file_path())
    try:
        if path.is_file() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, str(path) + ".1")
        return open(path, "a", encoding="utf-8")
    except OSError:
        return None


def debounce_seconds() -> float:
    try:
        return max(float(_env("FEIGE_DEBOUNCE_SECONDS") or 0), 0.0)
    except ValueError:
        return 0.0


def debounce_wait(key: str, seconds: float) -> bool:
    """尾沿去抖：同一 key（会话）在 seconds 内又有新的收尾，就放弃本次发送。

    Stop / agent-turn-complete 每轮回复都会触发，而统计口径是全会话累计的——
    连续多轮时只有最后一张卡有意义。做法：写入本次的戳 → 睡 seconds → 戳还是自己
    才发。返回 True 表示应发送；戳文件读写失败时照发（fail-open）。
    """
    stamp = Path(tempfile.gettempdir()) / "feige-debounce" / hashlib.sha1(
        key.encode("utf-8")).hexdigest()[:16]
    token = f"{os.getpid()}-{time.time_ns()}"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(token, encoding="utf-8")
    except OSError:
        return True
    time.sleep(seconds)
    try:
        return stamp.read_text(encoding="utf-8") == token
    except OSError:
        return True


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
                      help="发送渠道（默认：有飞书 webhook 走 feishu-webhook，否则有应用凭据走 feishu-cardkit）")
    send.add_argument("--chat-id", default="",
                      help="目标群：cardkit 用 oc_xxx（默认 FEISHU_NOTIFY_CHAT_ID）、telegram 用群 id（默认 TELEGRAM_CHAT_ID）")
    send.add_argument("--webhook", default="",
                      help="渠道级 webhook 覆盖（feishu-webhook / dingtalk-webhook；默认读各渠道环境变量）")
    send.add_argument("--route", action="store_true",
                      help="按 --project 解析路由文件（FEIGE_ROUTES_FILE，默认 ~/.feige-routes.json）多目标 fanout；"
                           "路由不可用退化为单渠道")
    send.add_argument("--dry-run", action="store_true",
                      help="只打印将发送的载荷 JSON，不发网络请求")
    send.add_argument("--debounce-key", default="",
                      help="收尾去抖键（通常是 agent:会话 id）；同键在去抖窗口内又有新发送则放弃本次")
    send.add_argument("--debounce", type=float, default=None,
                      help="去抖窗口秒数（默认读 FEIGE_DEBOUNCE_SECONDS，0 关闭；dry-run 不去抖）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:  # Windows 终端 emoji/中文输出兜底
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.command == "send":
        wait = args.debounce if args.debounce is not None else debounce_seconds()
        if args.debounce_key and wait > 0 and not args.dry_run:
            if not debounce_wait(args.debounce_key, wait):
                log(f"superseded by a newer stop within {wait:g}s, skipped")
                return 0
        stats = {
            "project": args.project, "model": args.model,
            "thinking": args.thinking, "tools": args.tools,
            "context": args.context, "elapsed": args.elapsed,
        }
        if args.route:
            ok, detail = send_routed(args.title, args.body, stats=stats,
                                     status=args.status, project=args.project,
                                     dry_run=args.dry_run)
        else:
            ok, detail = send_report(args.title, args.body, stats=stats, status=args.status,
                                     channel=args.channel, chat_id=args.chat_id,
                                     webhook=args.webhook, dry_run=args.dry_run)
        print(detail if args.dry_run else (f"✅ {detail}" if ok else f"❌ {detail}"))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
