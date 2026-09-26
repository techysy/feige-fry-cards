#!/usr/bin/env python3
"""🕊️ feige-fry-cards WebUI —— 独立设置页（M5）。

    python webui.py [--port 8787]

纯 Python 标准库（http.server 手写），只绑 127.0.0.1——无鉴权，切勿对外暴露端口。
另校验 Host（防 DNS rebinding）与 POST 的 Origin/Referer（防 CSRF），见 request_allowed。

页面：
  GET  /          总览：4 渠道凭据状态（只显"已配置/未配置"，绝不回显密钥值）、
                  路由文件状态、仓库路径；附各渠道测试发送按钮（未配置禁用）
  GET+POST /routes 群路由表单化编辑（校验 + .bak 备份 + 原子写；$ENV 引用原样保留）
  GET  /preview   卡片预览：feige dry-run 载荷 JSON + 朴素 HTML 外观示意
  POST /test      测试发送一张固定内容卡片（结果打码后展示）
  GET  /adapters  接入状态自检（ZCode/Claude Code/Codex，只读，绝不写用户配置）

secrets 硬规则（与 feige.redact 同级）：任何响应体不得出现 FEISHU_APP_SECRET /
bot token / webhook key 的值。例外：群路由页为可编辑会回显路由文件里的明文值——
路由里的密钥请写成 $ENV 引用。
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
VERSION = "0.7.0"

_CSS = """
body{font-family:"Microsoft YaHei",system-ui,sans-serif;max-width:860px;margin:0 auto;padding:12px;color:#24292f;background:#f6f8fa}
nav{margin-bottom:14px}nav a{margin-right:14px;color:#0969da;text-decoration:none}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px;margin-bottom:14px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #d0d7de;padding:6px 8px;text-align:left;font-size:14px}
input,select,textarea{padding:5px;font-size:14px;border:1px solid #d0d7de;border-radius:4px}
input[type=text]{width:100%;box-sizing:border-box}textarea{width:100%;box-sizing:border-box}
button{padding:6px 14px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer}
button:disabled{opacity:.4;cursor:not-allowed}
.ok{color:#1a7f37;font-weight:bold}.bad{color:#d1242f;font-weight:bold}
.err{background:#ffebe9;border:1px solid #ff8182;border-radius:8px;padding:10px;margin-bottom:14px}
.msg{background:#dafbe1;border:1px solid #2da44e;border-radius:8px;padding:10px;margin-bottom:14px}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px;overflow:auto;font-size:13px}
.bar{height:6px;border-radius:3px 3px 0 0}
.small{font-size:12px;color:#57606a}
"""


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def page(title: str, body: str) -> bytes:
    nav = ('<nav><a href="/">总览</a><a href="/routes">群路由</a>'
           '<a href="/preview">卡片预览</a><a href="/adapters">接入自检</a></nav>')
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>🕊️ feige · {esc(title)}</title><style>{_CSS}</style></head>'
            f'<body>{nav}<h2>🕊️ feige · {esc(title)}</h2>{body}'
            f'<p class="small">feige-fry-cards v{VERSION} · {esc(REPO)} · 仅 127.0.0.1</p>'
            f'</body></html>').encode("utf-8")


def channel_status() -> dict:
    """4 渠道凭据状态：只报 True/False，绝不取值。"""
    return {
        "feishu-webhook": bool(feige.webhook_url()),
        "feishu-cardkit": bool(feige._env("FEISHU_APP_ID", "LARK_APP_ID")
                               and feige._env("FEISHU_APP_SECRET", "LARK_APP_SECRET")),
        "dingtalk-webhook": bool(feige._env("DINGTALK_WEBHOOK")),
        "telegram": bool(feige._env("TELEGRAM_BOT_TOKEN") and feige._env("TELEGRAM_CHAT_ID")),
    }


# ── 页面：总览 ────────────────────────────────────────────────────────────────

def page_overview(msg: str = "") -> bytes:
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
    banner = f'<div class="msg">{esc(msg)}</div>' if msg else ""
    body = (f"{banner}<div class='card'><h3>渠道状态</h3>"
            f"<table><tr><th>渠道</th><th>凭据</th><th>测试</th></tr>{rows}</table>"
            f"<p class='small'>凭据只显示配置与否，密钥值与 webhook 内容永不回显。</p></div>"
            f"<div class='card'><h3>群路由</h3><p>{routes_info}</p>"
            f"<p><a href='/routes'>编辑路由 →</a></p></div>")
    return page("总览", body)


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
 `<td><input type="text" name="cid_${i}_${j}" placeholder="oc_… / -100…"></td>`+
 `<td><input type="text" name="tok_${i}_${j}" placeholder="$ENV 或 token"></td>`+
 `<td><input type="text" name="sec_${i}_${j}" placeholder="$ENV（签名/加签密钥）"></td></tr>`;}
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
    return page("群路由", body)


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
    return page("卡片预览", form + result_html)


# ── POST：测试发送 ────────────────────────────────────────────────────────────

def handle_test_post(body: bytes) -> bytes:
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    channel = (form.get("channel") or [""])[0]
    status = channel_status()
    if channel not in feige.CHANNELS or not status.get(channel):
        return page_overview(msg=f"渠道 {channel or '?'} 未配置，已跳过测试发送")
    ok, detail = feige.send_report(
        "🕊️ feige WebUI 测试卡",
        "这是一张来自 feige WebUI 的固定内容测试卡——收到即说明该渠道凭据与版面均正常。",
        stats={"project": "feige-fry-cards", "model": "", "thinking": 0,
               "tools": 0, "context": "", "elapsed": ""},
        status="ok", channel=channel)
    msg = f"[{channel}] 测试卡{'发送成功' if ok else '发送失败'}：{feige.redact(detail)}"
    return page_overview(msg=msg)


# ── 页面：接入状态自检 ────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def page_adapters(home: Path | None = None) -> bytes:
    """三家的接入方式都读各自真实的安装记录（插件优先，手动配置兜底），只读不写。"""
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

    body = "<div class='card'><table><tr><th>agent</th><th>状态</th><th>说明 / 指引</th></tr>"
    for name, note, ok, guide in rows:
        body += (f"<tr><td>{esc(name)}</td>"
                 f"<td class={'ok' if ok else 'bad'}>{'已接入' if ok else '未接入'}</td>"
                 f"<td>{note}<br><span class='small'>→ {esc(guide)}</span></td></tr>")
    body += "</table><p class='small'>本页只读检测，绝不写任何用户配置文件。</p></div>"
    return page("接入状态自检", body)


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
    server_version = "feige-webui/0.6"

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
                return page_overview()
            if u.path == "/routes":
                return _routes_form(feige.load_routes())
            if u.path == "/preview":
                return page_preview(q)
            if u.path == "/adapters":
                return page_adapters()
            return page("404", "<div class='err'>页面不存在</div>")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if u.path == "/routes":
            return handle_routes_post(body)
        if u.path == "/test":
            return handle_test_post(body)
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
    ap = argparse.ArgumentParser(description="🕊️ feige WebUI（仅 127.0.0.1，勿暴露端口）")
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
    print(f"🕊️ feige WebUI: http://127.0.0.1:{args.port} （Ctrl+C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("bye")


if __name__ == "__main__":
    main()
