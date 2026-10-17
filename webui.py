#!/usr/bin/env python3
"""🕊️ feige Studio —— 设置页（M9，对齐 fry-cards Studio 体验）。

    python webui.py [--port 8787]

配置 / 预览 / 状态 三个标签页：渠道凭据、正文总结 LLM、行为开关全部表单化，
保存写 ~/.feige-settings.json（原子写 + .bak，只写本页白名单键，其余原样保留）。
feige._env 统一「环境变量 > 设置文件」优先级——保存即生效（后台发送进程每次重新
读取），不必再配任何环境变量；环境变量若已设置则优先，页面上标出来源。

页面：
  GET  /          配置：分卡片表单（飞书 / 钉钉 / Telegram / 正文总结 / 行为）
  GET  /preview   预览：feige dry-run 载荷 JSON + 朴素 HTML 外观示意
  GET  /status    状态：渠道凭据状态（含来源）、路由/总结概况、测试发送、接入自检入口
  GET+POST /routes 群路由表单化编辑（校验 + .bak 备份 + 原子写；$ENV 引用原样保留）
  POST /settings  保存配置卡片（form=<卡片 id>，按卡片白名单更新）
  POST /test      测试发送一张固定内容卡片（结果打码后展示）
  GET  /adapters  接入状态自检（ZCode/Claude Code/Codex/kimi-code，只读）

secrets 硬规则（与 feige.redact 同级）：任何响应体不得出现渠道密钥 / bot token /
webhook key / FEIGE_LLM_API_KEY 的值——密钥与 webhook 输入框永远是空 password 框，
只显「已配置」徽章；留空 = 不修改，勾选「清除」才删除。例外：群路由页为可编辑会
回显路由文件里的明文值——路由里的密钥请写成 $ENV 引用。

纯 Python 标准库（http.server 手写），只绑 127.0.0.1——无鉴权，切勿对外暴露端口。
另校验 Host（防 DNS rebinding）与 POST 的 Origin/Referer（防 CSRF），见 request_allowed。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feige  # noqa: E402

REPO = Path(__file__).resolve().parent
VERSION = "0.9.0"

_CSS = """
body{font-family:"Microsoft YaHei",system-ui,sans-serif;max-width:1080px;margin:0 auto;
padding:14px;color:#24292f;background:#f6f8fa}
header.top{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
header.top h1{font-size:20px;margin:0}
header.top .ver{color:#57606a;font-size:12px}
nav{margin-bottom:14px;border-bottom:2px solid #d0d7de}
nav a{display:inline-block;padding:6px 14px;margin-right:4px;color:#57606a;
text-decoration:none;border-radius:6px 6px 0 0;font-size:14px}
nav a.on{background:#fff;color:#24292f;font-weight:bold;border:1px solid #d0d7de;
border-bottom:none;margin-bottom:-1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px;margin-bottom:14px}
.card h3{margin:0 0 10px;font-size:15px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #d0d7de;
padding:6px 8px;text-align:left;font-size:14px}
input,select,textarea{padding:5px;font-size:14px;border:1px solid #d0d7de;border-radius:4px}
input[type=text],input[type=password]{width:100%;box-sizing:border-box}
textarea{width:100%;box-sizing:border-box}
button{padding:6px 14px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer}
button:hover{background:#f6f8fa}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:#1f883d;border-color:#1a7f37;color:#fff}
.ok{color:#1a7f37;font-weight:bold}.bad{color:#d1242f;font-weight:bold}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
background:#eaeef2;color:#57606a}
.badge.on{background:#dafbe1;color:#1a7f37}
.err{background:#ffebe9;border:1px solid #ff8182;border-radius:8px;padding:10px;margin-bottom:14px}
.msg{background:#dafbe1;border:1px solid #2da44e;border-radius:8px;padding:10px;margin-bottom:14px}
.warn{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:10px;
margin-bottom:14px;font-size:13px}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px;
overflow:auto;font-size:13px}
.bar{height:6px;border-radius:3px 3px 0 0}
.small{font-size:12px;color:#57606a}
label.row{display:block;margin:6px 0}
label.row span{display:inline-block;min-width:130px;color:#57606a;font-size:13px}
.frow{margin:8px 0}
.frow .lab{font-size:13px;color:#24292f;margin-bottom:2px}
.frow .sub{font-size:12px;color:#57606a}
.frow input[type=text],.frow input[type=password]{margin-top:2px}
.clearline{font-size:12px;color:#57606a;margin-top:2px}
"""

# 设置文件键 → 对应环境变量（来源展示与回显用；feige._SETTINGS_ENV 的反向表）
_ENV_FOR_KEY = {v: k for k, v in feige._SETTINGS_ENV.items()}
# 密钥类键：页面永不回显值，输入框空 password + 留空不修改 + 清除勾选
_SECRET_KEYS = {"feishu_webhook", "feishu_webhook_secret", "feishu_app_secret",
                "dingtalk_webhook", "dingtalk_secret", "telegram_bot_token",
                "llm_api_key"}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def page(title: str, body: str, tab: str = "") -> bytes:
    def a(href: str, key: str, label: str) -> str:
        cls = " class='on'" if tab == key else ""
        return f"<a href='{href}'{cls}>{label}</a>"
    nav = ("<nav>" + a("/", "config", "⚙️ 配置") + a("/preview", "preview", "👁️ 预览")
           + a("/status", "status", "📊 状态") + a("/routes", "routes", "🧭 群路由")
           + a("/adapters", "adapters", "🔌 接入自检") + "</nav>")
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>🕊️ feige Studio · {esc(title)}</title><style>{_CSS}</style></head>'
            f'<body><header class="top"><h1>🕊️ feige Studio</h1>'
            f'<span class="ver">v{VERSION} · 配置 <code>{esc(feige.settings_file_path())}</code></span>'
            f'</header>{nav}{body}'
            f'<p class="small">feige-fry-cards v{VERSION} · {esc(REPO)} · 仅 127.0.0.1</p>'
            f'</body></html>').encode("utf-8")


def key_source(skey: str) -> str:
    """键的生效来源：环境变量（优先）/ 设置文件 / 空（未配置）。"""
    env = _ENV_FOR_KEY.get(skey)
    if env and os.environ.get(env, "").strip():
        return "环境变量"
    if str(feige.load_settings().get(skey) or "").strip():
        return "设置文件"
    return ""


# ── 表单控件小件 ─────────────────────────────────────────────────────────────

def _field(skey: str, label: str, placeholder: str = "", hint: str = "") -> str:
    src = key_source(skey)
    badge = (f"<span class='badge on'>已配置 · {esc(src)}</span>" if src
             else "<span class='badge'>未配置</span>")
    if skey in _SECRET_KEYS:
        field = (f"<input type='password' name='{skey}' autocomplete='new-password' "
                 f"placeholder='留空不修改'>")
        clear = (f"<div class='clearline'><label><input type='checkbox' "
                 f"name='clear_{skey}' value='1'> 清除已存值</label></div>")
    else:
        cur = feige._env(_ENV_FOR_KEY[skey]) if skey in _ENV_FOR_KEY else ""
        field = (f"<input type='text' name='{skey}' value='{esc(cur)}' "
                 f"placeholder='{esc(placeholder)}'>")
        clear = ""
    sub = f" <span class='sub'>{esc(hint)}</span>" if hint else ""
    return (f"<div class='frow'><div class='lab'>{esc(label)} {badge}{sub}</div>"
            f"{field}{clear}</div>")


def _save_btn(card_id: str) -> str:
    return (f"<p><input type='hidden' name='form' value='{card_id}'>"
            f"<button class='primary' type='submit'>💾 保存本卡片</button></p>")


def _banner(msg: str, err: str) -> str:
    out = ""
    if err:
        out += f'<div class="err">⚠️ {esc(err)}</div>'
    if msg:
        out += f'<div class="msg">✅ {esc(msg)}</div>'
    return out


# ── 页面：配置（Studio 主页）──────────────────────────────────────────────────

_WARN = ('<div class="warn">💾 保存只写本页白名单键进 <code>~/.feige-settings.json</code>'
         '（写前自动备份 <code>.bak</code>），其余内容原样保留；保存即生效——后台发送'
         '进程每次发卡重新读取，无需重启宿主。<br>🔐 密钥与 webhook 只显「已配置」徽章，'
         '输入框永不回显：留空 = 不修改，勾选「清除」才删除。已配环境变量时环境变量优先'
         '（徽章标注来源），此处改动对被遮蔽的键不生效。</div>')


def page_studio(msg: str = "", err: str = "") -> bytes:
    llm = feige.llm_config()
    llm_state = ("<span class='badge on'>已启用</span>" if llm["enabled"]
                 else "<span class='badge'>未启用（正文超长直接截断）</span>")
    cards = {
        "feishu": (
            "<div class='card'><h3>📲 飞书渠道</h3><form method='post' action='/settings'>"
            + _field("feishu_webhook", "自定义机器人 webhook",
                     "https://open.feishu.cn/open-apis/bot/v2/hook/…", "填了就走 webhook 通道")
            + _field("feishu_webhook_secret", "webhook 签名密钥", hint="机器人开「签名校验」才需要")
            + _field("feishu_app_id", "App ID（CardKit）", "cli_…", "不用 webhook 时填")
            + _field("feishu_app_secret", "App Secret（CardKit）")
            + _field("feishu_base_url", "飞书域名", feige.DEFAULT_BASE_URL, "Lark 国际版填 open.larksuite.com")
            + _field("feishu_chat_id", "接收群 chat_id", "oc_…", "CardKit 通道必配")
            + _save_btn("feishu") + "</form></div>"),
        "dingtalk": (
            "<div class='card'><h3>📎 钉钉渠道</h3><form method='post' action='/settings'>"
            + _field("dingtalk_webhook", "机器人 webhook")
            + _field("dingtalk_secret", "加签密钥", hint="SEC 开头，安全设置选「加签」才需要")
            + "<p class='small'>钉钉无交互卡，发 markdown 消息；需显式 <code>--channel</code> "
              "或群路由指定。</p>"
            + _save_btn("dingtalk") + "</form></div>"),
        "telegram": (
            "<div class='card'><h3>✈️ Telegram 渠道</h3><form method='post' action='/settings'>"
            + _field("telegram_bot_token", "Bot token", hint="BotFather 发放，形如 123456:ABC-…")
            + _field("telegram_chat_id", "目标 chat", "-100…（群）或个人 id")
            + "<p class='small'>纯文本版式（≤4096 UTF-16 单元）；需显式渠道或路由指定。</p>"
            + _save_btn("telegram") + "</form></div>"),
        "llm": (
            f"<div class='card'><h3>🧠 正文总结（LLM）{llm_state}</h3>"
            "<form method='post' action='/settings'>"
            + _field("llm_api_key", "API key", "OpenAI 兼容 chat/completions；配了才启用")
            + _field("llm_base_url", "Base URL", feige.DEFAULT_LLM_BASE_URL)
            + _field("llm_model", "模型", feige.DEFAULT_LLM_MODEL)
            + _field("llm_max_chars", "摘要目标字数", str(feige.LLM_MAX_CHARS), "≥50")
            + "<div class='frow'><label><input type='checkbox' name='llm_off' value='1' "
              + ("checked" if feige.load_settings().get("summarize") is False else "")
              + "> 显式关闭总结（即使配了 key 也只截断）</label></div>"
            + "<p class='small'>正文超长时先由后台发送进程调 LLM 浓缩成战报摘要，"
              "未配置/失败退化为硬截断；宿主 hook 前台零网络阻塞。</p>"
            + _save_btn("llm") + "</form></div>"),
        "behavior": (
            "<div class='card'><h3>🛠️ 行为开关</h3><form method='post' action='/settings'>"
            + _select_field("stats_scope", "统计口径",
                            {"turn": "单轮（💭/🔧/🎫/⏱️ 只算最后一轮）",
                             "session": "全程（整个会话累计）"},
                            feige.stats_scope())
            + _field("debounce_seconds", "收尾去抖秒数", "0", "同会话 N 秒内又收尾只发最后一张；0=每轮都发")
            + _field("routes_file", "群路由文件路径", "~/.feige-routes.json", "留空用默认")
            + "<p class='small'>宿主 hook 的启用开关（hook_notify）与凭据在各自插件设置里"
              "也有入口——插件设置注入为环境变量，优先于本页。</p>"
            + _save_btn("behavior") + "</form></div>"),
    }
    body = (_banner(msg, err) + _WARN
            + "<div class='grid'>" + cards["feishu"] + cards["llm"]
            + cards["dingtalk"] + cards["telegram"] + cards["behavior"] + "</div>"
            + "<div class='card'><h3>🧭 群路由</h3><p class='small'>项目 → 多群 fanout "
              "（钉钉 / Telegram 的凭据配在这里）。路由文件里的密钥建议写 <code>$ENV</code> 引用。</p>"
              "<p><a href='/routes'>编辑群路由 →</a></p></div>")
    return page("配置", body, tab="config")


def _select_field(skey: str, label: str, options: dict, current: str) -> str:
    src = key_source(skey)
    badge = (f"<span class='badge on'>已配置 · {esc(src)}</span>" if src
             else "<span class='badge'>默认</span>")
    opts = "".join(f"<option value='{esc(k)}' {'selected' if current == k else ''}>{esc(v)}</option>"
                   for k, v in options.items())
    return (f"<div class='frow'><div class='lab'>{esc(label)} {badge}</div>"
            f"<select name='{skey}'>{opts}</select></div>")


# ── POST /settings：按卡片白名单保存 ─────────────────────────────────────────

_CARD_KEYS = {
    "feishu": ["feishu_webhook", "feishu_webhook_secret", "feishu_app_id",
               "feishu_app_secret", "feishu_base_url", "feishu_chat_id"],
    "dingtalk": ["dingtalk_webhook", "dingtalk_secret"],
    "telegram": ["telegram_bot_token", "telegram_chat_id"],
    "llm": ["llm_api_key", "llm_base_url", "llm_model", "llm_max_chars"],
    "behavior": ["stats_scope", "debounce_seconds", "routes_file"],
}
_CARD_TITLES = {"feishu": "飞书渠道", "dingtalk": "钉钉渠道", "telegram": "Telegram",
                "llm": "正文总结", "behavior": "行为开关"}


def _set_text(form: dict, settings: dict, key: str) -> None:
    """普通键：有值写入，空值删除（回默认）。"""
    v = (form.get(key) or [""])[0].strip()
    if v:
        settings[key] = v
    else:
        settings.pop(key, None)


def _set_secret(form: dict, settings: dict, key: str) -> None:
    """密钥键：勾选清除才删；有值才写；留空保持现状。"""
    if (form.get("clear_" + key) or [""])[0]:
        settings.pop(key, None)
        return
    v = (form.get(key) or [""])[0].strip()
    if v:
        settings[key] = v


def apply_settings_card(form: dict, settings: dict) -> str:
    """纯函数：卡片表单 → 更新 settings（原地）；返回错误信息，空串成功。"""
    card = (form.get("form") or [""])[0]
    if card not in _CARD_KEYS:
        return f"未知设置卡片：{card or '(空)'}"
    for key in _CARD_KEYS[card]:
        if key == "stats_scope":
            v = (form.get(key) or [""])[0].strip().lower()
            if not v:
                continue  # 表单没带这个键（手搓请求）：保持现状
            if v not in feige._STATS_SCOPES:
                return f"非法统计口径：{v}"
            settings[key] = v
            continue
        if key in _SECRET_KEYS:
            _set_secret(form, settings, key)
            continue
        if key == "llm_max_chars":
            v = (form.get(key) or [""])[0].strip()
            if v:
                try:
                    v = str(max(int(v), 50))
                except ValueError:
                    return f"非法摘要目标字数：{v}（须为 ≥50 的整数）"
                settings[key] = v
            else:
                settings.pop(key, None)
            continue
        if key == "debounce_seconds":
            v = (form.get(key) or [""])[0].strip()
            if v:
                try:
                    v = str(max(float(v), 0.0))
                except ValueError:
                    return f"非法去抖秒数：{v}（须为数字）"
                settings[key] = v
            else:
                settings.pop(key, None)
            continue
        _set_text(form, settings, key)
    if card == "llm":
        if (form.get("llm_off") or [""])[0]:
            settings["summarize"] = False
        else:
            settings.pop("summarize", None)  # 回到「跟随 key 配置」
    return ""


def handle_settings_post(body: bytes) -> bytes:
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    settings = dict(feige.load_settings())  # 拷贝：load_settings 返回的是缓存对象，不能原地改
    err = apply_settings_card(form, settings)
    if err:
        return page_studio(err=err)
    path = Path(feige.settings_file_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            shutil.copy2(path, str(path) + ".bak")  # 先备份
        tmp = str(path) + ".tmp"
        Path(tmp).write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(tmp, path)  # 原子写
    except OSError as exc:
        return page_studio(err=f"设置保存失败：{feige.redact(exc)}")
    card = (form.get("form") or [""])[0]
    return page_studio(msg=f"{_CARD_TITLES[card]}已保存")


# ── 页面：状态 ────────────────────────────────────────────────────────────────

def channel_status() -> dict:
    """4 渠道凭据状态：只报 True/False，绝不取值。"""
    return {
        "feishu-webhook": bool(feige.webhook_url()),
        "feishu-cardkit": bool(feige._env("FEISHU_APP_ID", "LARK_APP_ID")
                               and feige._env("FEISHU_APP_SECRET", "LARK_APP_SECRET")),
        "dingtalk-webhook": bool(feige._env("DINGTALK_WEBHOOK")),
        "telegram": bool(feige._env("TELEGRAM_BOT_TOKEN") and feige._env("TELEGRAM_CHAT_ID")),
    }


def page_status(msg: str = "") -> bytes:
    status = channel_status()
    rows = "".join(
        f"<tr><td><code>{esc(ch)}</code></td>"
        f"<td class={'ok' if ok else 'bad'}>{'已配置' if ok else '未配置'}</td>"
        f"<td><form method='post' action='/test' style='margin:0'>"
        f"<input type='hidden' name='channel' value='{esc(ch)}'>"
        f"<button {'disabled' if not ok else ''}>发送测试卡</button></form></td></tr>"
        for ch, ok in status.items())
    routes_path = feige.routes_file_path()
    doc = feige.load_routes()
    n_targets = sum(len(v) for v in (doc or {}).get("routes", {}).values()
                    if isinstance(v, list)) if doc else 0
    routes_info = (f"路径 <code>{esc(routes_path)}</code> · "
                   f"{'存在，' + (str(len(doc.get('routes', {}))) + ' 个项目 · ' + str(n_targets) + ' 个目标') if doc else '不存在（按需到「群路由」页创建）'}")
    llm = feige.llm_config()
    llm_info = (f"{'🟢 已启用' if llm['enabled'] else '⚪ 未启用'} · 模型 "
                f"<code>{esc(llm['model'])}</code> · base <code>{esc(llm['base_url'])}</code> · "
                f"目标 ≤{llm['max_chars']} 字 · key 来源："
                f"{esc(key_source('llm_api_key') or '未配置')}")
    banner = f'<div class="msg">{esc(msg)}</div>' if msg else ""
    body = (f"{banner}<div class='card'><h3>渠道凭据</h3>"
            f"<table><tr><th>渠道</th><th>凭据</th><th>测试</th></tr>{rows}</table>"
            f"<p class='small'>凭据只显示配置与否（环境变量或设置文件），密钥值与 webhook "
            f"内容永不回显。配置请去「配置」页。</p></div>"
            f"<div class='card'><h3>正文总结（LLM）</h3><p>{llm_info}</p></div>"
            f"<div class='card'><h3>群路由</h3><p>{routes_info}</p>"
            f"<p><a href='/routes'>编辑路由 →</a></p></div>"
            f"<div class='card'><h3>行为</h3><p>统计口径：<code>{esc(feige.stats_scope())}</code> · "
            f"去抖：<code>{feige.debounce_seconds():g}s</code> · "
            f"日志：<code>{esc(feige.log_file_path())}</code></p></div>"
            f"<div class='card'><h3>接入自检</h3><p class='small'>ZCode / Claude Code / "
            f"Codex / kimi-code 的 hook 安装状态。</p><p><a href='/adapters'>查看接入自检 →</a></p></div>")
    return page("状态", body, tab="status")


# ── 页面：群路由编辑 ──────────────────────────────────────────────────────────

def _routes_form(doc: dict, msg: str = "", err: str = "") -> bytes:
    routes = (doc or {}).get("routes", {})
    blocks = []
    for i, (proj, targets) in enumerate(sorted(routes.items())):
        trows = "".join(
            "<tr>"
            f"<td><select name='ch_{i}_{j}'>" + "".join(
                f"<option {'selected' if t.get('channel') == c else ''}>{c}</option>"
                for c in feige.CHANNELS) + "</select></td>"
            f"<td><input type='text' name='wh_{i}_{j}' value='{esc(t.get('webhook', ''))}' placeholder='$ENV 或 webhook'></td>"
            f"<td><input type='text' name='cid_{i}_{j}' value='{esc(t.get('chat_id', ''))}' placeholder='oc_… / -100…'></td>"
            f"<td><input type='text' name='tok_{i}_{j}' value='{esc(t.get('token', ''))}' placeholder='$ENV 或 token'></td>"
            f"<td><input type='text' name='sec_{i}_{j}' value='{esc(t.get('secret', ''))}' placeholder='$ENV（签名/加签密钥）'></td>"
            "</tr>"
            for j, t in enumerate(targets if isinstance(targets, list) else []))
        blocks.append(
            f"<div class='card'><h3>项目 <input type='text' name='proj_{i}' value='{esc(proj)}' "
            f"style='width:280px'></h3>"
            f"<table id='tbl_{i}'><tr><th>channel</th><th>webhook</th><th>chat_id</th><th>token</th><th>secret</th></tr>"
            f"{trows}</table>"
            f"<button type='button' onclick=\"addTarget({i})\">＋目标</button></div>")
    n = len(routes)
    banner = f'<div class="msg">{esc(msg)}</div>' if msg else ""
    error = f'<div class="err">{esc(err)}</div>' if err else ""
    js = """
<script>
const CH=["feishu-webhook","feishu-cardkit","dingtalk-webhook","telegram"];
function trow(i,j){return `<tr><td><select name="ch_${i}_${j}">`+
 CH.map(c=>`<option>${c}</option>`).join("")+`</select></td>`+
 `<td><input type="text" name="wh_${i}_${j}" placeholder="$ENV 或 webhook"></td>`+
 `<td><input type="text" name="cid_${i}_{j}" placeholder="oc_… / -100…"></td>`+
 `<td><input type="text" name="tok_${i}_{j}" placeholder="$ENV 或 token"></td>`+
 `<td><input type="text" name="sec_${i}_{j}" placeholder="$ENV（签名/加签密钥）"></td></tr>`;}
function addTarget(i){const t=document.getElementById("tbl_"+i);
 t.insertAdjacentHTML("beforeend",trow(i,t.rows.length-1));}
function addProject(){const f=document.getElementById("rt");const i=window._n++;
 f.insertAdjacentHTML("beforeend",`<div class="card"><h3>项目 <input type="text" name="proj_${i}" style="width:280px"></h3>`+
 `<table id="tbl_${i}"><tr><th>channel</th><th>webhook</th><th>chat_id</th><th>token</th><th>secret</th></tr></table>`+
 `<button type="button" onclick="addTarget(${i})">＋目标</button></div>`);addTarget(i);}
</script>"""
    body = (f"{banner}{error}<form method='post' action='/routes'><div id='rt'>"
            + "".join(blocks) +
            f"</div><button type='button' onclick='addProject()'>＋项目</button> "
            f"<button type='submit'>💾 保存</button></form>"
            f"<p class='small'>channel 必填；整行留空会被丢弃。整字符串 <code>$NAME</code> "
            f"表示引用环境变量，保存时原样保留不展开。保存前自动备份为 .bak。</p>"
            f"<script>window._n={n};</script>{js}")
    return page("群路由", body, tab="routes")


def _parse_routes_form(form: dict) -> tuple[dict | None, str]:
    """表单 → 路由 doc；校验失败返回 (None, 错误信息)。"""
    doc: dict = {"routes": {}}
    proj_keys = sorted((k for k in form if k.startswith("proj_")),
                       key=lambda k: int(k.split("_", 1)[1] or 0))
    for pk in proj_keys:
        i = pk.split("_", 1)[1]
        name = (form.get(pk) or [""])[0].strip()
        targets = []
        j = 0
        # 行存在性：ch/wh/cid/tok 任一键在都算有这行——避免"有内容但无 ch 键"被静默丢弃
        while any(f"{k}_{i}_{j}" in form for k in ("ch", "wh", "cid", "tok", "sec")):
            ch = (form.get(f"ch_{i}_{j}") or [""])[0].strip()
            wh = (form.get(f"wh_{i}_{j}") or [""])[0].strip()
            cid = (form.get(f"cid_{i}_{j}") or [""])[0].strip()
            tok = (form.get(f"tok_{i}_{j}") or [""])[0].strip()
            sec = (form.get(f"sec_{i}_{j}") or [""])[0].strip()
            if ch or wh or cid or tok or sec:
                if ch not in feige.CHANNELS:
                    return None, f"项目 {name or pk} 第 {j + 1} 个目标缺 channel 或渠道非法（行内有内容时 channel 必填）"
                t = {"channel": ch}
                if wh:
                    t["webhook"] = wh
                if cid:
                    t["chat_id"] = cid
                if tok:
                    t["token"] = tok
                if sec:
                    t["secret"] = sec
                targets.append(t)
            j += 1
        if not name:
            if targets:
                return None, "存在未填项目名的目标行，请补上项目名（或留 \"*\" 表示兜底）"
            continue
        doc["routes"][name] = targets
    return doc, ""


def handle_routes_post(body: bytes) -> bytes:
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    doc, err = _parse_routes_form(form)
    if err:
        return _routes_form(feige.load_routes(), err=err)
    # 冒烟：保存后语义与 feige.resolve_routes 一致，先 dry 一遍计数
    def _smoke(p: str) -> str:
        targets = feige.resolve_routes(p, doc)
        bad = sum(1 for t in targets if t.get("_unresolved"))
        return f"{p}: {len(targets)} 目标" + (f"（{bad} 个引用了未设置的 $ENV，发送时将跳过）" if bad else "")
    smoke = ", ".join(_smoke(p) for p in doc["routes"])
    path = feige.routes_file_path()
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            shutil.copy2(p, str(p) + ".bak")  # 先备份
        tmp = str(p) + ".tmp"
        Path(tmp).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(tmp, p)  # 原子写
    except OSError as exc:
        return _routes_form(feige.load_routes(),
                            err=f"保存失败：{feige.redact(exc)}")
    return _routes_form(doc, msg=f"已保存（冒烟：{smoke or '空路由'}），原文件备份为 .bak")


# ── 页面：卡片预览 ────────────────────────────────────────────────────────────

_STATUS_COLOR = {"running": "#0969da", "ok": "#1a7f37", "error": "#d1242f"}


def page_preview(q: dict) -> bytes:
    def gv(key: str, default: str = "") -> str:
        return (q.get(key) or [default])[0]

    result_html = ""
    if gv("go"):
        status = gv("status", "ok")
        if status not in feige.STATUS_TEMPLATE:
            status = "ok"
        stats = {"project": gv("project"), "model": gv("model"),
                 "thinking": int(gv("thinking") or 0), "tools": int(gv("tools") or 0),
                 "context": gv("context"), "elapsed": gv("elapsed")}
        ok, detail = feige.send_report(gv("title", "标题"), gv("body", "正文"),
                                       stats=stats, status=status,
                                       channel=gv("channel", ""), dry_run=True)
        footer = feige.stats_footer(stats)
        mock = (
            f"<div class='card' style='padding:0;overflow:hidden;max-width:420px'>"
            f"<div class='bar' style='background:{_STATUS_COLOR[status]}'></div>"
            f"<div style='padding:12px'><b>{feige.STATUS_PREFIX[status]} {esc(gv('title', '标题'))}</b>"
            f"<pre style='white-space:pre-wrap;border:none;background:none'>{esc(gv('body', '正文'))}</pre>"
            + (f"<hr><p class='small'>{esc(footer)}</p>"
               if status != "running" and footer else "")
            + "</div></div>")
        result_html = (f"<div class='card'><h3>外观示意（非 1:1）</h3>{mock}</div>"
                       f"<div class='card'><h3>dry-run 载荷 JSON</h3>"
                       f"{'<pre>' + esc(detail) + '</pre>' if ok else '<div class=err>' + esc(detail) + '</div>'}</div>")
    opts = "".join(f"<option {'selected' if gv('channel') == c else ''}>{c}</option>"
                   for c in feige.CHANNELS)
    sopts = "".join(f"<option {'selected' if gv('status', 'ok') == s else ''}>{s}</option>"
                    for s in ("ok", "error", "running"))
    form = (f"<div class='card'><form method='get' action='/preview'>"
            f"<input type='hidden' name='go' value='1'>"
            f"<p>标题 <input type='text' name='title' value='{esc(gv('title', '任务完成'))}' style='width:300px'> "
            f"渠道 <select name='channel'>{opts}</select> "
            f"状态 <select name='status'>{sopts}</select></p>"
            f"<p>正文<br><textarea name='body' rows='4'>{esc(gv('body', '**一句话战报**'))}</textarea></p>"
            f"<p>项目 <input type='text' name='project' value='{esc(gv('project'))}' style='width:160px'> "
            f"模型 <input type='text' name='model' value='{esc(gv('model'))}' style='width:160px'> "
            f"💭 <input type='text' name='thinking' value='{esc(gv('thinking'))}' style='width:60px'> "
            f"🔧 <input type='text' name='tools' value='{esc(gv('tools'))}' style='width:60px'></p>"
            f"<p>上下文 <input type='text' name='context' value='{esc(gv('context'))}' style='width:160px'> "
            f"⏱️ <input type='text' name='elapsed' value='{esc(gv('elapsed'))}' style='width:100px'> "
            f"<button type='submit'>预览</button></p></form></div>")
    return page("预览", form + result_html, tab="preview")


# ── POST：测试发送 ────────────────────────────────────────────────────────────

def handle_test_post(body: bytes) -> bytes:
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    channel = (form.get("channel") or [""])[0]
    status = channel_status()
    if channel not in feige.CHANNELS or not status.get(channel):
        return page_status(msg=f"渠道 {channel or '?'} 未配置，已跳过测试发送")
    ok, detail = feige.send_report(
        "🕊️ feige Studio 测试卡",
        "这是一张来自 feige Studio 的固定内容测试卡——收到即说明该渠道凭据与版面均正常。",
        stats={"project": "feige-fry-cards", "model": "", "thinking": 0,
               "tools": 0, "context": "", "elapsed": ""},
        status="ok", channel=channel)
    msg = f"[{channel}] 测试卡{'发送成功' if ok else '发送失败'}：{feige.redact(detail)}"
    return page_status(msg=msg)


# ── 页面：接入状态自检 ────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def page_adapters(home: Path | None = None) -> bytes:
    """四家的接入方式都读各自真实的安装记录（插件优先，手动配置兜底），只读不写。"""
    home = home or Path.home()
    rows = []
    plugin = "feige-fry-cards"

    z_ok = plugin in _read(home / ".zcode" / "cli" / "plugins" / "installed_plugins.json")
    rows.append(("ZCode", "已安装 feige-fry-cards 插件" if z_ok
                 else "未安装插件（ZCode 插件市场添加本仓库目录后安装）",
                 z_ok, "README「插件安装 → ZCode」"))

    c_plugin = plugin in _read(home / ".claude" / "plugins" / "installed_plugins.json")
    settings = _read(home / ".claude" / "settings.json")
    c_manual = "stop_notify.py" in settings or "hooks/stop.py" in settings
    c_note = ("已安装 feige-fry-cards 插件" if c_plugin
              else "已手动配置 Stop hook" if c_manual
              else "未接入（/plugin marketplace add 本仓库目录后安装）")
    rows.append(("Claude Code", c_note, c_plugin or c_manual, "README「插件安装 → Claude Code」"))

    toml = _read(home / ".codex" / "config.toml")
    notify = next((ln.strip() for ln in toml.splitlines() if ln.strip().startswith("notify")), "")
    x_plugin = f'"{plugin}@' in toml
    x_notify = "feige" in notify.replace("\\", "/")
    if x_plugin:
        x_note = "已安装 feige-fry-cards 插件"
    elif x_notify:
        x_note = f"notify 已指向 feige：{esc(notify[:80])}"
    elif notify:
        x_note = (f"未接入；notify 已被占用：{esc(notify[:80])}——推荐装插件（与 notify 互不干扰），"
                  f"或用 notify.py 包装链把原命令接在后面")
    else:
        x_note = "未接入（codex plugin marketplace add 本仓库目录后安装）"
    rows.append(("Codex CLI", x_note, x_plugin or x_notify, "README「插件安装 → Codex」"))

    k_cfg = _read(home / ".kimi-code" / "config.toml")
    k_ok = "stop_notify.py" in k_cfg.replace("\\", "/") or "hooks/stop.py" in k_cfg.replace("\\", "/")
    rows.append(("kimi-code", "已手挂 Stop hook" if k_ok
                 else "未接入（~/.kimi-code/config.toml 手挂，见 README「插件安装 → kimi-code」）",
                 k_ok, "README「插件安装 → kimi-code」"))

    body = ("<div class='card'><table><tr><th>agent</th><th>状态</th><th>说明 / 指引</th></tr>")
    for name, note, ok, guide in rows:
        body += (f"<tr><td>{esc(name)}</td>"
                 f"<td class={'ok' if ok else 'bad'}>{'已接入' if ok else '未接入'}</td>"
                 f"<td>{note}<br><span class='small'>→ {esc(guide)}</span></td></tr>")
    body += ("</table><p class='small'>本页只读检测，绝不写任何用户配置文件。"
             "hook 跑在 agent 启动时的环境快照里——改过环境变量请重启宿主。</p></div>")
    return page("接入自检", body, tab="adapters")


# ── HTTP 骨架 ─────────────────────────────────────────────────────────────────

def request_allowed(method: str, host: str, origin: str, referer: str, port: int) -> str:
    """只绑 127.0.0.1 挡不住浏览器：返回拒绝原因，空串表示放行。

    - Host 必须是本机地址（防 DNS rebinding：恶意域名解析到 127.0.0.1 后读路由页）；
    - POST 的 Origin（缺省退到 Referer）必须同源（防 CSRF：任意网页自动提交表单
      改写路由、把战报导向攻击者 webhook）。两者都缺的非浏览器客户端（curl）放行。
    """
    local = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if (host or "").strip().lower() not in local:
        return f"Host 不被允许：{host or '(缺失)'}"
    if method == "POST":
        src = (origin or "").strip()
        if not src and referer:
            u = urllib.parse.urlsplit(referer)
            src = f"{u.scheme}://{u.netloc}"
        if src and src.lower() not in {f"http://{h}" for h in local}:
            return f"跨站请求被拒绝：{src}"
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "feige-webui/0.9"

    def log_message(self, fmt, *args):  # 静默访问日志（避免路径里的业务参数刷屏）
        pass

    def _send(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self) -> bytes:
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query or "")
        if self.command == "GET":
            if u.path in ("/", ""):
                return page_studio()
            if u.path == "/preview":
                return page_preview(q)
            if u.path == "/status":
                return page_status()
            if u.path == "/routes":
                return _routes_form(feige.load_routes())
            if u.path == "/adapters":
                return page_adapters()
            return page("404", "<div class='err'>页面不存在</div>")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if u.path == "/routes":
            return handle_routes_post(body)
        if u.path == "/test":
            return handle_test_post(body)
        if u.path == "/settings":
            return handle_settings_post(body)
        return page("404", "<div class='err'>页面不存在</div>")

    def _handle(self) -> None:
        reason = request_allowed(self.command, self.headers.get("Host", ""),
                                 self.headers.get("Origin", ""),
                                 self.headers.get("Referer", ""),
                                 self.server.server_address[1])
        if reason:
            self._send(page("拒绝", f"<div class='err'>{esc(reason)}</div>"), status=403)
            return
        try:
            self._send(self._dispatch())
        except Exception as exc:  # fail-open：错误条而不是 500 白屏
            try:
                self._send(page("出错", f"<div class='err'>处理失败：{esc(feige.redact(exc))}</div>"))
            except Exception:
                pass

    do_GET = do_POST = _handle


class Server(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许重复绑定已被占用的端口：不报错，请求随机落到
    # 某个进程上。在 Windows 上关掉它，端口冲突时直接报错退出。
    allow_reuse_address = os.name != "nt"


def main() -> None:
    ap = argparse.ArgumentParser(description="🕊️ feige Studio（仅 127.0.0.1，勿暴露端口）")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        srv = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        sys.exit(f"🕊️ 端口 {args.port} 不可用（{exc}），换一个：python webui.py --port <N>")
    print(f"🕊️ feige Studio: http://127.0.0.1:{args.port} （Ctrl+C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("bye")


if __name__ == "__main__":
    main()
