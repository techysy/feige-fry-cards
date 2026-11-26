#!/usr/bin/env python3
"""🕊️ feige Studio —— 设置页（M9，对齐 fry-cards Studio 体验）。

    python webui.py [--port 8787]           # 前台跑（终端关了就没了）
    python webui.py start  [--port 8787]    # 后台保活：脱离终端的守护进程拉起 worker，
    python webui.py stop                    #   worker 崩溃自动重启；日志 ~/.feige-studio.log
    python webui.py restart
    python webui.py status

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
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feige  # noqa: E402
import feige_interactive  # noqa: E402

REPO = Path(__file__).resolve().parent
VERSION = feige.__version__  # 面板版本号与主插件同源，发版只改 feige.__version__ + 清单

# 视觉语言对齐 CreditDaddy / ImgMark（两者同一套：CSS 变量双主题
# html[data-theme="dark"]、980px 内容列（宽屏 1400px）、胶囊导航、14px 圆角卡片、
# 深色主按钮）。
_CSS = """
:root{
  --bg:#f5f6f8; --card:#ffffff; --surface-2:#fafafa; --tertiary:#f0f0f0;
  --hover:#f5f6f8; --active:#eef0f3;
  --line:#eeeeee; --border-subtle:#eeeeee; --border:#e5e5e5; --border-strong:#cccccc;
  --txt:#1f1f1f; --muted:#5f6368; --text-3:#999999;
  --btn-bg:#1f1f1f; --btn-fg:#ffffff;
  --pri:#22c55e; --pri-dk:#16a34a; --ok:#16a34a; --bad:#dc2626; --warn-c:#d97706;
  --radius:14px;
  --col-w:980px; /* 内容列宽：宽屏（≥1420px）放宽到 1400px 让卡片栅格出 3 列 */
}
html[data-theme="dark"]{
  --bg:#0a0a0c; --card:#131316; --surface-2:#17171b; --tertiary:#1b1b20;
  --hover:rgba(255,255,255,.06); --active:rgba(255,255,255,.1);
  --line:rgba(255,255,255,.13); --border-subtle:#202025; --border:rgba(255,255,255,.13);
  --border-strong:#3a3a42;
  --txt:#e6e6e9; --muted:rgba(230,230,233,.72); --text-3:rgba(230,230,233,.5);
  --btn-bg:rgba(255,255,255,.92); --btn-fg:#0a0a0c;
  --pri:#7f77dd; --pri-dk:#8f88e2; --ok:#2ee59d; --bad:#ff6b6b; --warn-c:#f59e0b;
}
*{box-sizing:border-box}
body{margin:0;font:13px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",
  "Microsoft YaHei","Segoe UI",sans-serif;background:var(--bg);color:var(--txt);
  min-height:100vh;-webkit-font-smoothing:antialiased;transition:background .2s}
button,input,select,textarea{font-family:inherit;font-size:inherit;color:inherit}
a{color:var(--pri-dk)}
.muted,.small{color:var(--text-3);font-size:12px}

/* header：品牌左、工具右，与导航/内容同收 980px 居中列（宽屏 1400px） */
header{display:flex;align-items:center;justify-content:space-between;gap:12px;
  max-width:var(--col-w);margin:0 auto;padding:16px 22px 4px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.brand .logo-emoji{font-size:26px;line-height:1}
.brand .title{font-family:"Avenir Next",Avenir,"Segoe UI Variable Display","Segoe UI",sans-serif;
  font-size:17px;font-weight:750;letter-spacing:-.5px;line-height:1.2;color:var(--txt)}
.brand .ver{color:var(--text-3);font-size:11px;line-height:1.3}
.head-right{display:flex;align-items:center;gap:10px}
.icon-btn{width:28px;height:28px;border-radius:8px;display:inline-flex;align-items:center;
  justify-content:center;border:1px solid transparent;background:var(--hover);
  color:var(--muted);cursor:pointer;transition:all .15s;padding:0;font-size:14px;
  text-decoration:none}
.icon-btn:hover{background:var(--active);color:var(--txt)}

/* 胶囊导航：容器卡底 + 激活深底白字（CreditDaddy .nav 同款），
   左缘对齐卡片左缘（居中列外缘 + 22px 内边距），窄屏退到 22px */
.nav{display:flex;gap:4px;padding:4px;width:fit-content;max-width:calc(100% - 44px);
  margin:10px auto 0;background:var(--card);border:1px solid var(--border-subtle);
  border-radius:12px;overflow-x:auto}
@supports (margin:10px 0 0 max(22px,1px)){
  .nav{margin:10px 0 0 max(22px,calc((100% - var(--col-w))/2 + 22px))}}
.nav a{display:inline-flex;align-items:center;gap:6px;border:none;background:transparent;
  color:var(--muted);font-size:13px;font-weight:600;padding:7px 14px;border-radius:9px;
  cursor:pointer;white-space:nowrap;text-decoration:none;transition:background .15s,color .15s}
.nav a:hover{color:var(--txt);background:var(--hover)}
.nav a.on{background:var(--btn-bg);color:var(--btn-fg)}

main{max-width:var(--col-w);margin:0 auto;padding:12px 22px 30px;display:flex;
  flex-direction:column;gap:14px}
/* 瀑布流多列：行式 grid 的行高被该行最高的卡撑开，矮卡（钉钉）下面吊一大块空白，
   整页被顶出超一屏；多列布局每列独立堆叠、卡片按序流入最矮列，总高≈最高列。
   列数仍随宽度自适应：980px 列 2 列、宽屏 1400px 列 3 列 */
.grid{columns:420px;column-gap:14px}
.grid>.card{break-inside:avoid;margin:0 0 14px}
/* 宽屏：内容列 1400px，420px 列宽放得下 3 列（3×420 + 间距 + 内边距 ≈ 1332px） */
@media (min-width:1420px){:root{--col-w:1400px}}

.card{background:var(--card);border:1px solid var(--border-subtle);
  border-radius:var(--radius);padding:14px 16px}
.card h2,.card h3{margin:0 0 12px;font-size:13px;font-weight:700}
.card h3 small{color:var(--text-3);font-weight:normal;font-size:12px;margin-left:6px}

table{border-collapse:collapse;width:100%}
td,th{border:1px solid var(--border-subtle);padding:6px 8px;text-align:left;font-size:12px}
th{background:var(--surface-2);color:var(--muted);font-weight:600}

input[type=text],input[type=password],input[type=number],select,textarea{
  padding:6px 8px;border:1px solid var(--border);border-radius:8px;font-size:13px;
  background:var(--surface-2);color:var(--txt)}
input[type=text],input[type=password],textarea{width:100%}
textarea{resize:vertical;font-family:inherit}
input[type=checkbox]{accent-color:var(--pri)}
select{cursor:pointer}

/* 按钮：元素级基础样式（CreditDaddy .btn 观感），.primary 为深色主按钮。
   .icon-btn 是 class 选择器，优先级高于 button，主题切换钮不受影响。 */
button{display:inline-flex;align-items:center;justify-content:center;gap:5px;
  padding:6px 11px;border:1px solid var(--border);border-radius:8px;
  background:var(--surface-2);color:var(--muted);font-size:12px;font-weight:600;
  cursor:pointer;transition:all .15s;line-height:1.2;white-space:nowrap;text-decoration:none}
button:hover{background:var(--hover);border-color:var(--border-strong);color:var(--txt)}
button.primary{background:var(--btn-bg);border-color:var(--btn-bg);color:var(--btn-fg)}
button.primary:hover{opacity:.88}
button:disabled{opacity:.45;cursor:not-allowed}
button.tiny{padding:3px 10px;font-size:12px}

.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;
  background:var(--tertiary);color:var(--text-3)}
.tag.on{background:color-mix(in srgb,var(--pri) 12%,transparent);color:var(--pri-dk)}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}

.err{background:color-mix(in srgb,var(--bad) 8%,var(--card));
  border:1px solid color-mix(in srgb,var(--bad) 35%,transparent);
  border-radius:12px;padding:10px 12px;font-size:13px}
.msg{background:color-mix(in srgb,var(--ok) 10%,var(--card));
  border:1px solid color-mix(in srgb,var(--ok) 35%,transparent);
  border-radius:12px;padding:10px 12px;font-size:13px}
.warn{background:color-mix(in srgb,var(--warn-c) 9%,var(--card));
  border:1px solid color-mix(in srgb,var(--warn-c) 30%,transparent);
  border-radius:12px;padding:10px 12px;font-size:12px;line-height:1.7}
/* 提示文案非宽屏单行截断（两行提示挤四排太占高度），宽屏恢复完整换行 */
.warn-line{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (min-width:1420px){.warn-line{white-space:normal;overflow:visible}}

pre{background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:10px;
  padding:10px;overflow:auto;font:12px/1.6 Consolas,"Cascadia Mono",monospace}
code{font:12px/1.4 Consolas,"Cascadia Mono",monospace;background:var(--tertiary);
  padding:1px 5px;border-radius:5px}
/* 预览页卡片外观与 CardKit v2.0 仿真容器（对齐 fry-cards Studio） */
.bar{height:6px;border-radius:12px 12px 0 0}
.pv-panel{border:1px solid var(--border);border-radius:8px;padding:6px 10px;margin:10px 0;background:var(--surface-2)}
.pv-panel.b-green{border-color:#22c55e66}.pv-panel.b-red{border-color:#ef444466}.pv-panel.b-blue{border-color:#3b82f666}
.pv-panel summary{cursor:pointer;font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;list-style:none}
.pv-panel summary::-webkit-details-marker{display:none}
.vlogo{width:16px;height:16px;border-radius:3px;object-fit:contain;vertical-align:middle;flex-shrink:0}
.pv-form{border:1px dashed var(--border-strong);border-radius:8px;padding:8px;margin-top:8px;background:var(--surface-2)}
.pv-btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}

/* 可折叠渠道卡 & 联调折叠提示 & 状态胶囊（学习 hermes-fry-cards Studio） */
details.fold-card>summary.fold-summary{list-style:none;cursor:pointer;display:flex;align-items:center;
  justify-content:space-between;gap:10px;font-size:13px;font-weight:700;user-select:none}
details.fold-card>summary.fold-summary::-webkit-details-marker{display:none}
.fold-title{display:inline-flex;align-items:center;gap:4px;min-width:0}
.fold-right{display:inline-flex;align-items:center;gap:8px;flex-shrink:0}
.fold-badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.fold-badge.on{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.fold-badge.empty{background:var(--tertiary);color:var(--text-3)}
.fold-caret{width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;
  border-top:5px solid var(--text-3);transition:transform .16s}
details.fold-card[open]>summary .fold-caret{transform:rotate(180deg)}
details.fold-card .fold-body{margin-top:12px;padding-top:12px;border-top:1px solid var(--border-subtle)}

details.hint-fold{margin:8px 0 12px;background:var(--surface-2);border:1px solid var(--border-subtle);
  border-radius:10px;padding:8px 10px}
details.hint-fold>summary{cursor:pointer;font-size:12px;font-weight:600;color:var(--muted);
  list-style:none;display:flex;align-items:center;gap:6px}
details.hint-fold>summary::-webkit-details-marker{display:none}
details.hint-fold .hint-body{margin-top:8px;font-size:12px;color:var(--muted);line-height:1.65}
details.hint-fold ol{margin:4px 0 0 18px;padding:0}

.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;
  font-size:11px;font-weight:600;white-space:nowrap}
.badge.ok{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.badge.bad{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.badge.warn{background:color-mix(in srgb,var(--warn-c) 15%,transparent);color:var(--warn-c)}
.badge.mute{background:var(--tertiary);color:var(--text-3)}

/* 真实 CardKit v2.0 预览器布局与白底飞书仿真卡片 */
.pv-grid{display:grid;grid-template-columns:minmax(320px,450px) 1fr;gap:14px;align-items:start}
@media (max-width:920px){.pv-grid{grid-template-columns:1fr}}
.preset-pills{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.preset-pill{font-size:11px;padding:4px 9px;border-radius:999px;border:1px solid var(--border);
  background:var(--surface-2);color:var(--txt);cursor:pointer;transition:all .15s}
.preset-pill:hover{border-color:var(--pri);background:color-mix(in srgb,var(--pri) 9%,transparent)}
.preset-pill.on{border-color:var(--pri);background:var(--btn-bg);color:var(--btn-fg)}

.pv-stage-wrap{background:linear-gradient(135deg,color-mix(in srgb,var(--surface-2) 90%,transparent),
  color-mix(in srgb,var(--tertiary) 75%,transparent));border:1px solid var(--border-subtle);
  border-radius:12px;padding:16px;display:flex;flex-direction:column;align-items:center;gap:12px}
.feishu-card{width:100%;max-width:480px;background:#ffffff;color:#1f2329;border:1px solid #dee0e3;
  border-radius:12px;overflow:hidden;box-shadow:0 6px 20px rgba(0,0,0,.08);text-align:left;font-size:13px}
.fc-header{padding:11px 14px;font-size:15px;font-weight:700;display:flex;align-items:center;gap:6px;
  border-bottom:1px solid rgba(0,0,0,.05)}
.fc-header.t-blue{background:linear-gradient(90deg,#e8f3ff,#f4f8ff);color:#1456f0}
.fc-header.t-green{background:linear-gradient(90deg,#e8f8ef,#f4fbf7);color:#117a43}
.fc-header.t-red{background:linear-gradient(90deg,#ffece8,#fff6f5);color:#c9352f}
.fc-header.t-grey{background:linear-gradient(90deg,#f2f3f5,#fafafa);color:#4e5969}
.fc-body{padding:12px 14px;display:flex;flex-direction:column;gap:9px}
.fc-md{line-height:1.58;word-break:break-word;color:#1f2329}
.fc-md p{margin:0 0 6px}.fc-md p:last-child{margin-bottom:0}
.fc-md h4,.fc-md h5{margin:4px 0;font-size:13px;font-weight:700;color:#1f2329}
.fc-md code{background:#f2f3f5;color:#1f2329;padding:1px 4px;border-radius:4px;font-size:12px}
.fc-md pre{background:#f5f6f7;color:#1f2329;border:1px solid #e5e6eb;padding:8px;border-radius:6px;margin:4px 0;overflow:auto}
.fc-md pre code{background:none;padding:0}
.fc-md table{margin:4px 0;border-collapse:collapse;width:100%;font-size:12px}
.fc-md th,.fc-md td{border:1px solid #dee0e3;padding:4px 7px;background:#fff;color:#1f2329}
.fc-md th{background:#f5f6f7;font-weight:600}
.fc-md ul,.fc-md ol{margin:4px 0 4px 18px;padding:0}
.fc-md blockquote{margin:4px 0;padding:2px 8px;border-left:3px solid #c9cdd4;color:#646a73}
.fc-hr{border:none;border-top:1px solid #ebedf0;margin:2px 0}
.feishu-card .pv-panel{background:#f8f9fa;border:1px solid #dee0e3;margin:0}
.feishu-card .pv-panel.b-green{border-color:#8ce0b3}.feishu-card .pv-panel.b-red{border-color:#f9a7a4}
.feishu-card .pv-panel.b-blue{border-color:#93c5fd}
.feishu-card .pv-panel summary{color:#4e5969;justify-content:space-between}
.fc-sum-left{display:inline-flex;align-items:center;gap:6px;min-width:0}
.fc-sum-arrow{color:#8f959e;font-size:14px;transition:transform .16s}
details.pv-panel[open] .fc-sum-arrow{transform:rotate(90deg)}
.fc-panel-body{margin-top:8px;padding-top:8px;border-top:1px dashed #e5e6eb;color:#1f2329;font-size:12px}
.fc-form{background:#f7f8fa;border:1px solid #e5e6eb;border-radius:8px;padding:9px;display:flex;flex-direction:column;gap:7px}
.fc-input{width:100%;padding:6px 8px;border:1px solid #d0d3d6;border-radius:6px;background:#fff;color:#1f2329;font-size:12px}
.fc-actions{display:flex;flex-wrap:wrap;gap:6px}
.fc-btn{border:1px solid #d0d3d6;background:#fff;color:#1f2329;border-radius:6px;padding:5px 10px;font-size:12px;font-weight:500;cursor:pointer}
.fc-btn:hover{background:#f2f3f5}
.fc-btn.primary{background:#1456f0;border-color:#1456f0;color:#fff}
.fc-btn.primary:hover{background:#0f46c7;opacity:1}
.fc-confirm-tag{font-size:10px;opacity:.75;margin-left:2px}
.fc-notation{font-size:11.5px;color:#8f959e;line-height:1.5}
.pv-toast{position:fixed;right:20px;bottom:20px;z-index:80;max-width:360px;background:#1f2329;color:#fff;
  padding:10px 14px;border-radius:10px;font-size:12px;box-shadow:0 10px 28px rgba(0,0,0,.25);display:none}
.pv-toast.show{display:block}

/* 表单行：12px 灰标签 + 徽章 + 全宽控件 + 环境变量锁定 + 复选框对齐 */
.frow{margin:0 0 12px}
.frow .lab{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
.frow .lab .sub{color:var(--text-3)}
.frow input[type=text],.frow input[type=password]{margin-top:0}
input.env-locked,textarea.env-locked,select.env-locked{opacity:.74;background:var(--tertiary);
  border-style:dashed;cursor:not-allowed}
.env-note{font-size:11px;color:var(--warn-c);margin-top:3px;line-height:1.4}
.clearline{font-size:11px;color:var(--text-3);margin-top:3px}
.clearline label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.checkrow{margin:0 0 11px;font-size:13px}
.check-top{display:flex;align-items:flex-start;gap:7px;line-height:1.45;cursor:pointer}
.check-top input[type=checkbox]{margin:3px 0 0;flex-shrink:0}
.check-main{flex:1;min-width:0}
.check-main .tag{margin-left:5px;vertical-align:1px}
.check-sub{display:block;font-size:11.5px;color:var(--text-3);margin:2px 0 0 21px;line-height:1.45}
.mode-banner{border:1px solid color-mix(in srgb,var(--warn-c) 35%,transparent);
  background:color-mix(in srgb,var(--warn-c) 9%,var(--card));border-radius:10px;
  padding:8px 10px;font-size:12px;margin-bottom:12px;line-height:1.55}
.mode-banner.ok-mode{border-color:color-mix(in srgb,var(--ok) 35%,transparent);
  background:color-mix(in srgb,var(--ok) 7%,var(--card))}
.dirty-dot{display:none;font-size:11px;color:var(--warn-c);font-weight:600;margin-left:auto}
form.dirty .dirty-dot{display:inline-flex;align-items:center;gap:4px}
form.dirty button[type=submit].primary{box-shadow:0 0 0 2px color-mix(in srgb,var(--warn-c) 45%,transparent)}
.cond-wrap{transition:opacity .16s}
.cond-wrap.dimmed{opacity:.52}
.card form{display:flex;flex-direction:column}
.card form p{margin:0 0 10px}
.card form p:last-child{margin-bottom:0}
footer{text-align:center;color:var(--text-3);font-size:12px;padding:0 18px 18px}

.hidden{display:none !important}
.preset-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.preset-picker{position:relative;flex:1 1 200px;min-width:160px}
.preset-toggle{width:100%;display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:6px 10px;border:1px solid var(--border);border-radius:8px;background:var(--card);
  color:var(--txt);cursor:pointer;text-align:left;transition:border-color .15s,background .15s}
.preset-toggle:hover{border-color:var(--border-strong);background:var(--hover)}
.preset-toggle.open{border-color:var(--pri)}
.preset-toggle .pt-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
.preset-toggle .pt-caret{width:0;height:0;flex-shrink:0;border-left:4px solid transparent;
  border-right:4px solid transparent;border-top:5px solid var(--text-3);transition:transform .15s}
.preset-toggle.open .pt-caret{transform:rotate(180deg)}
.preset-menu{position:absolute;z-index:40;top:calc(100% + 4px);left:0;right:0;max-height:340px;
  overflow-y:auto;background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:6px;box-shadow:0 12px 32px rgba(0,0,0,.16);display:flex;flex-direction:column;gap:3px}
#llm-model-filter{width:100%;box-sizing:border-box;margin-bottom:4px;padding:6px 8px;font-size:12px}
.mdl-group + .mdl-group{margin-top:8px}
.mdl-group-head{display:flex;justify-content:space-between;gap:8px;padding:3px 6px 2px;
  font-size:11px;font-weight:700;color:var(--muted)}
.mdl-group-head span{color:var(--text-3);font-weight:normal}
.mdl-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:4px}
.mdl-card{display:flex;flex-direction:column;gap:2px;width:100%;text-align:left;cursor:pointer;
  padding:6px 10px;border:1px solid transparent;border-radius:8px;background:var(--surface-2);color:var(--txt);
  transition:border-color .15s,background .15s}
.mdl-card:hover{background:var(--hover);border-color:var(--border-subtle)}
.mdl-card.on{border-color:var(--pri);background:color-mix(in srgb,var(--pri) 8%,transparent)}
.pt-item-head{display:flex;align-items:baseline;justify-content:space-between;gap:6px;width:100%}
.pt-item-name{font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pt-item-time{font-size:10px;color:var(--text-3);flex-shrink:0;font-variant-numeric:tabular-nums}
.pt-empty{padding:14px 8px;font-size:12px;color:var(--muted);text-align:center}
@media (max-width:600px){.preset-menu{width:min(340px,calc(100vw - 40px));}}

/* ---------- 字段 chips（点按显隐 + 拖动排序 — 对齐 fry-cards Studio / CreditDaddy） ---------- */
.field-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0 10px; }
.field-tip { color: var(--muted); font-size: 12px; margin-bottom: 4px; display: block; }
.field-chip {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--txt);
  border-radius: 999px; padding: 4px 12px 4px 8px; font-size: 12px; font-weight: 600;
  cursor: grab; user-select: none;
  transition: border-color .15s, background .15s, color .15s, opacity .15s;
}
.field-chip:hover { border-color: var(--border-strong); }
.field-chip .drag-handle { color: var(--text-3); flex-shrink: 0; }
.field-chip.off { background: var(--surface-2); color: var(--text-3); font-weight: 500; opacity: .75; border-style: dashed; }
.field-chip.dragging { opacity: .4; cursor: grabbing; }
.field-chip.drop-mark { border-left: 3px solid var(--pri); padding-left: 5px; }

/* SVG 线性图标标准样式 */
.ic { display: inline-block; vertical-align: middle; flex-shrink: 0; margin-right: 4px; }
"""

# 主题在样式表加载前确定，避免暗色用户看到白闪（对齐 ImgMark 的 head 引导脚本）
_THEME_BOOT = ("<script>document.documentElement.dataset.theme="
               "localStorage.getItem('feige_theme')||"
               "(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');</script>")
_THEME_JS = """
<script>
(function(){
  var b=document.getElementById('btn-theme');if(!b)return;
  var sunSvg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  var moonSvg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  function icon(){b.innerHTML=document.documentElement.dataset.theme==='dark'?sunSvg:moonSvg;}
  icon();
  b.onclick=function(){var n=document.documentElement.dataset.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=n;localStorage.setItem('feige_theme',n);icon();};
})();

// 模型拉取与下拉选择器逻辑（对齐 ImgMark）
(function(){
  var picker = document.getElementById('llm-model-picker');
  if(!picker) return;
  var input = document.getElementById('llm_model');
  var toggle = document.getElementById('llm-model-toggle');
  var menu = document.getElementById('llm-model-menu');
  var filter = document.getElementById('llm-model-filter');
  var list = document.getElementById('llm-model-list');
  var refresh = document.getElementById('llm-models-refresh');
  var models = [];
  var hasFetched = false;

  function escHtml(s){
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function modelProviderOf(m) {
    if (m.id.indexOf('/') !== -1) return m.id.split('/')[0];
    return m.owned_by || '其他';
  }

  function modelShortName(id) {
    if (id.indexOf('/') !== -1) return id.split('/').slice(1).join('/');
    return id;
  }

  function renderModels(){
    var q = (filter.value || '').trim().toLowerCase();
    var filtered = models.filter(function(m){
      return !q || m.id.toLowerCase().indexOf(q) !== -1 || (m.owned_by && m.owned_by.toLowerCase().indexOf(q) !== -1);
    });
    if(!filtered.length){
      list.innerHTML = '<div class="pt-empty">' + (models.length ? '没有匹配的模型' : '暂无模型，点击「拉取」获取') + '</div>';
      return;
    }
    var groups = {};
    filtered.forEach(function(m){
      var p = modelProviderOf(m);
      if(!groups[p]) groups[p] = [];
      groups[p].push(m);
    });
    var html = '';
    var currentVal = input.value.trim();
    Object.keys(groups).sort().forEach(function(prov){
      var ms = groups[prov];
      html += '<div class="mdl-group">';
      html += '<div class="mdl-group-head">' + escHtml(prov) + '<span>' + ms.length + ' 个</span></div>';
      html += '<div class="mdl-cards">';
      ms.forEach(function(m){
        var on = m.id === currentVal ? ' on' : '';
        var ctxStr = m.context_window ? '<span class="pt-item-time">' + Math.round(m.context_window/1000) + 'k</span>' : '';
        html += '<button type="button" class="mdl-card' + on + '" data-id="' + escHtml(m.id) + '">';
        html += '<div class="pt-item-head"><span class="pt-item-name" title="' + escHtml(m.id) + '">' + escHtml(modelShortName(m.id)) + '</span>' + ctxStr + '</div>';
        html += '</button>';
      });
      html += '</div></div>';
    });
    list.innerHTML = html;
  }

  async function fetchModelsList(){
    list.innerHTML = '<div class="pt-empty">正在拉取模型列表…</div>';
    refresh.disabled = true;
    var baseInp = document.querySelector('input[name="llm_base_url"]');
    var keyInp = document.querySelector('input[name="llm_api_key"]');
    var baseUrl = (baseInp ? baseInp.value : '').trim();
    var apiKey = (keyInp ? keyInp.value : '').trim();

    try {
      var res = await fetch('/api/llm/models', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base_url: baseUrl, api_key: apiKey})
      });
      var data = await res.json().catch(function(){ return {}; });
      if(!res.ok || !data.ok){
        throw new Error(data.error || ('HTTP ' + res.status));
      }
      models = data.models || [];
      hasFetched = true;
      renderModels();
    } catch(err){
      list.innerHTML = '<div class="pt-empty" style="color:var(--bad)">拉取失败: ' + escHtml(err.message) + '</div>';
    } finally {
      refresh.disabled = false;
    }
  }

  toggle.onclick = function(e){
    e.preventDefault();
    e.stopPropagation();
    var open = menu.classList.toggle('hidden') === false;
    toggle.classList.toggle('open', open);
    if(open){
      filter.focus();
      if(!hasFetched) fetchModelsList();
      else renderModels();
    }
  };

  refresh.onclick = function(e){
    e.preventDefault();
    e.stopPropagation();
    fetchModelsList();
  };

  filter.oninput = renderModels;

  list.onclick = function(e){
    var card = e.target.closest('.mdl-card');
    if(!card) return;
    var mid = card.getAttribute('data-id');
    if(mid){
      input.value = mid;
      renderModels();
      menu.classList.add('hidden');
      toggle.classList.remove('open');
    }
  };

  document.addEventListener('click', function(e){
    if(e.target.closest('#llm-model-picker')) return;
    menu.classList.add('hidden');
    toggle.classList.remove('open');
  });
})();

// 字段 chips（点按显隐 + 拖动排序，对齐 fry-cards Studio / CreditDaddy）
// 面板头 / 脚注两套独立 chips，各自写各自的 hidden input
(function(){
  function dropRefChip(b, x, y, dragChip) {
    var chips = Array.prototype.slice.call(b.querySelectorAll('.field-chip')).filter(function(c){ return c !== dragChip; });
    for(var i=0; i<chips.length; i++){
      var r = chips[i].getBoundingClientRect();
      if(y > r.bottom) continue;
      if(y >= r.top && x >= r.left + r.width / 2) continue;
      return chips[i];
    }
    return null;
  }

  document.querySelectorAll('.field-chips[data-target]').forEach(function(box){
    var hiddenInput = document.getElementById(box.dataset.target);
    var dragChip = null;

    function updateValue(){
      var picked = [];
      box.querySelectorAll('.field-chip').forEach(function(c){
        if(!c.classList.contains('off')) picked.push(c.dataset.field);
      });
      if(hiddenInput) hiddenInput.value = picked.join(',');
    }

    box.addEventListener('click', function(e){
      var chip = e.target.closest('.field-chip');
      if(!chip) return;
      var off = chip.classList.toggle('off');
      chip.setAttribute('aria-pressed', off ? 'false' : 'true');
      updateValue();
    });

    box.addEventListener('dragstart', function(e){
      var chip = e.target.closest('.field-chip');
      if(!chip) { e.preventDefault(); return; }
      dragChip = chip;
      chip.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', chip.dataset.field);
    });
    box.addEventListener('dragover', function(e){
      if(!dragChip) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      box.querySelectorAll('.field-chip.drop-mark').forEach(function(c){ c.classList.remove('drop-mark'); });
      var ref = dropRefChip(box, e.clientX, e.clientY, dragChip);
      if(ref) ref.classList.add('drop-mark');
    });
    box.addEventListener('drop', function(e){
      if(!dragChip) return;
      e.preventDefault();
      var ref = dropRefChip(box, e.clientX, e.clientY, dragChip);
      if(ref) box.insertBefore(dragChip, ref);
      else box.appendChild(dragChip);
      updateValue();
    });
    box.addEventListener('dragleave', function(e){
      if(e.relatedTarget && box.contains(e.relatedTarget)) return;
      box.querySelectorAll('.field-chip.drop-mark').forEach(function(c){ c.classList.remove('drop-mark'); });
    });
    box.addEventListener('dragend', function(){
      box.querySelectorAll('.field-chip.drop-mark').forEach(function(c){ c.classList.remove('drop-mark'); });
      if(dragChip) dragChip.classList.remove('dragging');
      dragChip = null;
    });
  });
})();

// Studio 配置页交互增强：未保存脏检测 + 「交互与长连接」条件展开联动
(function(){
  document.querySelectorAll("form[action='/settings']").forEach(function(f){
    var markDirty = function(){ f.classList.add('dirty'); };
    f.addEventListener('input', markDirty);
    f.addEventListener('change', markDirty);
  });
  var cbInter = document.querySelector("input[name='feishu_interactive']");
  var cbWs = document.querySelector("input[name='feishu_ws_enabled']");
  var advWrap = document.getElementById('interactive-advanced-wrap');
  var advDetails = document.getElementById('interactive-advanced-fold');
  if(cbInter && cbWs && advWrap){
    function syncInterUI(){
      var active = cbInter.checked || cbWs.checked;
      advWrap.classList.toggle('dimmed', !active);
      if(advDetails && active && !advDetails.dataset.userClosed){
        advDetails.open = true;
      }else if(advDetails && !active){
        advDetails.open = false;
      }
    }
    if(advDetails){
      advDetails.addEventListener('toggle', function(){
        if(!advDetails.open && (cbInter.checked || cbWs.checked)){
          advDetails.dataset.userClosed = '1';
        }else if(advDetails.open){
          delete advDetails.dataset.userClosed;
        }
      });
    }
    cbInter.addEventListener('change', syncInterUI);
    cbWs.addEventListener('change', syncInterUI);
    syncInterUI();
  }
})();
</script>"""

# ── SVG 图标集（对齐 ImgMark / CreditDaddy：使用标准规范线性矢量图标，减少 Emoji） ──────
def _svg_icon(path_d: str, w: int = 14, h: int = 14, extra: str = "") -> str:
    return (f'<svg class="ic{(" " + extra) if extra else ""}" viewBox="0 0 24 24" width="{w}" height="{h}" '
            f'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{path_d}</svg>')

ICONS = {
    "dove": _svg_icon('<path d="M16 7h.01"/><path d="M3.4 18c1.3 0 2.6-.5 3.5-1.5L16 7.5a3.5 3.5 0 0 0-5-5L6 7.5A5 5 0 0 0 4.5 11c0 1.5.5 3 1.5 4.1L3.4 18z"/><path d="m14 10 5.5 5.5a2.5 2.5 0 0 1 0 3.5l-.5.5a2.5 2.5 0 0 1-3.5 0L10 14"/>', 18, 18),
    "config": _svg_icon('<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'),
    "preview": _svg_icon('<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>'),
    "status": _svg_icon('<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>'),
    "routes": _svg_icon('<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>'),
    "adapters": _svg_icon('<path d="M12 2v6"/><path d="m19 13-4-4"/><path d="M10 20v-6"/><path d="m5 13 4-4"/><path d="M15 11a4 4 0 0 1-6 0"/>'),
    "feishu": _svg_icon('<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>'),
    "dingtalk": _svg_icon('<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'),
    "telegram": _svg_icon('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>'),
    "llm": _svg_icon('<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>'),
    "behavior": _svg_icon('<line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="1" x2="7" y1="14" y2="14"/><line x1="9" x2="15" y1="8" y2="8"/><line x1="17" x2="23" y1="16" y2="16"/>'),
    "save": _svg_icon('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>'),
    "refresh": _svg_icon('<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/>'),
    "check": _svg_icon('<path d="M20 6 9 17l-5-5"/>'),
    "alert": _svg_icon('<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>'),
    "lock": _svg_icon('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
}

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
    nav = ("<nav class='nav'>" + a("/", "config", f"{ICONS['config']}配置")
           + a("/preview", "preview", f"{ICONS['preview']}预览")
           + a("/status", "status", f"{ICONS['status']}状态")
           + a("/routes", "routes", f"{ICONS['routes']}群路由")
           + a("/adapters", "adapters", f"{ICONS['adapters']}接入自检") + "</nav>")
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{_THEME_BOOT}'
            f'<title>feige Studio · {esc(title)}</title><style>{_CSS}</style></head>'
            f'<body><header><div class="brand"><span class="logo-emoji">🕊️</span>'
            f'<div><div class="title">feige Studio</div>'
            f'<div class="ver">v{VERSION} · 配置 <code>{esc(feige.settings_file_path())}</code></div></div></div>'
            f'<div class="head-right"><button class="icon-btn" id="btn-theme" '
            f'title="切换亮色 / 暗色"></button></div></header>'
            f'{nav}<main>{body}</main>'
            f'<footer>feige-fry-cards v{VERSION} · {esc(REPO)} · 仅 127.0.0.1</footer>'
            f'{_THEME_JS}</body></html>').encode("utf-8")


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
    env_name = _ENV_FOR_KEY.get(skey, "")
    locked = src == "环境变量"
    badge = (f"<span class='tag on'>已配置 · {esc(src)}</span>" if src
             else "<span class='tag'>未配置</span>")
    if skey in _SECRET_KEYS:
        if locked:
            field = (f"<input type='password' name='{skey}' disabled class='env-locked' "
                     f"autocomplete='new-password' placeholder='已由环境变量接管（优先于设置文件）'>")
        else:
            field = (f"<input type='password' name='{skey}' autocomplete='new-password' "
                     f"placeholder='留空不修改'>")
        clear = (f"<div class='clearline'><label><input type='checkbox' "
                 f"name='clear_{skey}' value='1'> 清除已存值</label></div>")
    else:
        cur = feige._env(env_name) if env_name else ""
        ro = " readonly class='env-locked' title='当前由环境变量接管，本页改动不会覆盖'" if locked else ""
        field = (f"<input type='text' name='{skey}' value='{esc(cur)}' "
                 f"placeholder='{esc(placeholder)}'{ro}>")
        clear = ""
    env_note = (f"<div class='env-note'>🔒 环境变量 <code>{esc(env_name)}</code> 生效中，此处改动不会覆盖它</div>"
                if locked and env_name else "")
    sub = f" <span class='sub'>{esc(hint)}</span>" if hint else ""
    return (f"<div class='frow'><div class='lab'>{esc(label)} {badge}{sub}</div>"
            f"{field}{clear}{env_note}</div>")



FOOTER_FIELD_NAMES = {
    "project": "项目",
    "model": "模型",
    "thinking": "思考数",
    "tools": "工具数",
    "context": "上下文",
    "tokens": "Tokens",
    "elapsed": "耗时",
}

def _field_chips_html(skey: str, label: str, defaults: tuple, tip: str = "") -> str:
    """一套字段 chips（点按显隐 + 拖动排序）。skey 对应设置键与 hidden input 名。"""
    saved = feige.load_settings().get(skey)
    if saved is None:
        saved_list = list(defaults)
    else:
        saved_list = [str(x).strip() for x in saved if str(x).strip()]

    chips = []
    active_keys = set()
    for k in saved_list:
        if k in FOOTER_FIELD_NAMES:
            chips.append((k, FOOTER_FIELD_NAMES[k], True))
            active_keys.add(k)
    for k in defaults:
        if k not in active_keys:
            chips.append((k, FOOTER_FIELD_NAMES[k], False))

    grip_svg = ('<svg class="drag-handle" viewBox="0 0 16 16" width="10" height="12" fill="currentColor" aria-hidden="true">'
                '<circle cx="5" cy="3.2" r="1.4"/><circle cx="11" cy="3.2" r="1.4"/>'
                '<circle cx="5" cy="8" r="1.4"/><circle cx="11" cy="8" r="1.4"/>'
                '<circle cx="5" cy="12.8" r="1.4"/><circle cx="11" cy="12.8" r="1.4"/></svg>')

    chips_html = []
    for k, name, is_on in chips:
        on_cls = "" if is_on else " off"
        aria_p = "true" if is_on else "false"
        chips_html.append(f'<button type="button" class="field-chip{on_cls}" draggable="true" '
                          f'data-field="{esc(k)}" aria-pressed="{aria_p}">'
                          f'{grip_svg}<span>{esc(name)}</span></button>')

    active_val = ",".join(k for k, _, is_on in chips if is_on)
    tip_html = f"<span class='field-tip'>{esc(tip)}</span>" if tip else ""
    return (
        "<div class='frow'>"
        f"<div class='lab'>{esc(label)} <span class='tag on'>可拖动排序</span></div>"
        "<span class='field-tip'>点按切换显隐（置灰 = 隐藏）；按住拖动调整展示顺序。</span>"
        + tip_html +
        f"<div class='field-chips' id='{esc(skey)}-chips-box' data-target='{esc(skey)}_val'>{''.join(chips_html)}</div>"
        f"<input type='hidden' name='{esc(skey)}' id='{esc(skey)}_val' value='{esc(active_val)}'>"
        "</div>"
    )


def _panel_chips_html() -> str:
    return _field_chips_html(
        "panel_fields", "统一面板头字段（panel）", feige.DEFAULT_PANEL_FIELDS,
        tip="卡片标题已含项目与宿主，面板头默认不重复项目名；与底部脚注各自独立。")


def _footer_chips_html() -> str:
    return _field_chips_html(
        "footer_fields", "底部脚注字段（footer）", feige.DEFAULT_FOOTER_FIELDS,
        tip="脚注行默认隐藏（面板头已承载同一套统计）；勾选上方开关才显示。")

def _save_btn(card_id: str, preview_btn: bool = False) -> str:
    extra = (f" <button type='submit' name='_next' value='preview'>{ICONS['preview']} 保存并预览</button>"
             if preview_btn else "")
    return (f"<p style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>"
            f"<input type='hidden' name='form' value='{card_id}'>"
            f"<button class='primary' type='submit'>{ICONS['save']} 保存本卡片</button>{extra}"
            f"<span class='dirty-dot'>● 未保存</span></p>")


def _fold_card(icon_html: str, title: str, configured: bool, inner_html: str) -> str:
    """对齐 fry-cards Studio 的次要配置折叠卡（已配置自动展开，未配置默认折叠）。"""
    badge = ("<span class='fold-badge on'>已配置</span>" if configured
             else "<span class='fold-badge empty'>未配置</span>")
    open_attr = " open" if configured else ""
    return (
        f"<div class='card'><details class='fold-card'{open_attr}>"
        f"<summary class='fold-summary'>"
        f"<span class='fold-title'>{icon_html} {esc(title)}</span>"
        f"<span class='fold-right'>{badge}<span class='fold-caret'></span></span>"
        f"</summary>"
        f"<div class='fold-body'>{inner_html}</div>"
        f"</details></div>"
    )


def _banner(msg: str, err: str) -> str:
    out = ""
    if err:
        out += f'<div class="err">{ICONS["alert"]} {esc(err)}</div>'
    if msg:
        out += f'<div class="msg">{ICONS["check"]} {esc(msg)}</div>'
    return out


# ── 页面：配置（Studio 主页）──────────────────────────────────────────────────

def _warn_html() -> str:
    return (f'<div class="warn"><span class="warn-line">{ICONS["save"]} 保存只写本页白名单键进 <code>~/.feige-settings.json</code>'
            '（写前自动备份 <code>.bak</code>），其余内容原样保留；保存即生效——后台发送'
            f'进程每次发卡重新读取，无需重启宿主。</span><span class="warn-line">{ICONS["lock"]} 密钥与 webhook 只显「已配置」徽章，'
            '输入框永不回显：留空 = 不修改，勾选「清除」才删除。已配环境变量时环境变量优先'
            '（徽章标注来源），此处改动对被遮蔽的键不生效。</span></div>')


def _checkbox_field(skey: str, label: str, checked: bool, hint: str = "") -> str:
    src = key_source(skey)
    env_name = _ENV_FOR_KEY.get(skey, "")
    badge = (f"<span class='tag on'>已配置 · {esc(src)}</span>" if src
             else "<span class='tag'>默认</span>")
    sub = f"<span class='check-sub'>{esc(hint)}</span>" if hint else ""
    env_note = (f"<span class='check-sub' style='color:var(--warn-c)'>🔒 环境变量 <code>{esc(env_name)}</code> 优先</span>"
                if src == "环境变量" and env_name else "")
    chk = "checked" if checked else ""
    return (f"<div class='checkrow'><label class='check-top'>"
            f"<input type='checkbox' name='{esc(skey)}' value='1' {chk}>"
            f"<span class='check-main'>{esc(label)} {badge}</span></label>{sub}{env_note}</div>")


def _ws_badge_html(ws: dict) -> str:
    ws_badge_map = {
        "running": "<span class='badge ok'>🟢 监听中</span>",
        "connecting": "<span class='badge warn'>🟡 连接中</span>",
        "stopped": "<span class='badge mute'>⚪ 未启动</span>",
        "unavailable": "<span class='badge warn'>🟠 缺少 lark-oapi</span>",
        "error": "<span class='badge bad'>🔴 连接异常</span>",
    }
    return ws_badge_map.get(ws.get("state", ""), f"<span class='badge mute'>{esc(ws.get('state', ''))}</span>")


def page_studio(msg: str = "", err: str = "") -> bytes:
    llm = feige.llm_config()
    llm_state = ("<span class='tag on'>已启用</span>" if llm["enabled"]
                 else "<span class='tag'>未启用（正文超长直接截断）</span>")
    cur_model = feige._env(_ENV_FOR_KEY.get("llm_model", "")) or ""
    allow_val = feige._env("FEIGE_ALLOW_USERS")
    allow_src = key_source("interactive_allow_users")
    allow_badge = (f"<span class='tag on'>已配置 · {esc(allow_src)}</span>" if allow_src
                   else "<span class='tag'>不限（建议配置）</span>")
    st_ch = channel_status()
    has_webhook = bool(st_ch.get("feishu-webhook"))
    has_cardkit = bool(st_ch.get("feishu-cardkit"))
    if has_webhook:
        mode_banner = ("<div class='mode-banner'>⚠️ <b>当前生效：自定义 Webhook 模式</b>"
                       "（优先级高于 App ID；交互卡片与长连接监听暂不生效）。若想开启交互卡片，请清空并清除下方的 Webhook。</div>")
    elif has_cardkit:
        mode_banner = ("<div class='mode-banner ok-mode'>🟢 <b>当前生效：CardKit 应用模式</b>"
                       "（支持交互追问卡片、长连接回调与厂商 Logo）。</div>")
    else:
        mode_banner = ("<div class='mode-banner'>尚未配置飞书通道凭据：推荐填写下方 <b>App ID / Secret + 接收群</b>"
                       "（或展开使用自定义 Webhook）。</div>")

    ws = feige_interactive.get_ws_manager().status()
    ws_badge = _ws_badge_html(ws)
    inter_on = feige.interactive_enabled()
    ws_on = feige.ws_enabled()
    adv_open = " open" if (inter_on or ws_on or allow_val.strip()) else ""
    adv_dim = "" if (inter_on or ws_on) else " dimmed"

    feishu_guide = (
        "<details class='hint-fold'>"
        "<summary>ℹ️ 查看飞书长连接（WSClient）与交互卡片联调说明</summary>"
        "<div class='hint-body'><ol>"
        "<li>确保当前环境已安装可选依赖 <code>pip install lark-oapi>=1.4.0</code>（未安装时仅关闭交互监听，不影响发卡）；</li>"
        "<li>在飞书开放平台应用「事件与回调 → 事件订阅方式」选择 <b>使用长连接接收事件</b>，并添加回调 <code>card.action.trigger</code>；</li>"
        "<li>确认权限包含发消息（<code>im:message:send_as_bot</code>）、上传图片（<code>im:resource</code>）、CardKit 卡片，并发布版本且将机器人拉入目标群；</li>"
        "<li>开启长连接后，建议填写「操作人白名单（<code>open_id</code>）」防群内误触，并用 <code>python webui.py start</code> 常驻保活。</li>"
        "</ol></div></details>"
    )
    webhook_fold = (
        f"<details class='fold-card' id='feishu-webhook-fold' style='margin:6px 0 12px'{' open' if has_webhook else ''}>"
        f"<summary class='fold-summary'>"
        f"<span class='fold-title'>自定义机器人 Webhook（静态覆盖）</span>"
        f"<span class='fold-right'><span class='fold-badge {'on' if has_webhook else 'empty'}'>"
        f"{'优先覆盖中' if has_webhook else '未启用'}</span><span class='fold-caret'></span></span>"
        f"</summary>"
        f"<div class='fold-body'>"
        + _field("feishu_webhook", "自定义机器人 webhook",
                 "https://open.feishu.cn/open-apis/bot/v2/hook/…", "填了就强制走静态 webhook（无按钮/表单交互）")
        + _field("feishu_webhook_secret", "webhook 签名密钥", hint="机器人开「签名校验」才需要")
        + "</div></details>"
    )
    cards = {
        "feishu": (
            f"<div class='card'><h3>{ICONS['feishu']} 飞书渠道 · 凭据</h3><form method='post' action='/settings'>"
            + mode_banner
            + _field("feishu_app_id", "App ID（CardKit）", "cli_…", "支持交互卡片、折叠面板与长连接")
            + _field("feishu_app_secret", "App Secret（CardKit）")
            + _field("feishu_chat_id", "接收群 chat_id", "oc_…", "CardKit 通道必配（全项目默认群）")
            + _field("feishu_base_url", "飞书域名", feige.DEFAULT_BASE_URL, "Lark 国际版填 open.larksuite.com")
            # 每群一个接收群：键名 feishu_chat_id__<项目名>，项目名必须与路由标签逐字一致
            + _field("feishu_chat_id__10router", "10Router 群 chat_id（按项目覆盖）", "oc_…",
                     "键名 feishu_chat_id__<项目名>：留空沿用默认群")
            + webhook_fold
            + _save_btn("feishu", preview_btn=True) + "</form></div>"),
        "interactive": (
            f"<div class='card'><h3 style='display:flex;align-items:center;justify-content:space-between;gap:8px'>"
            f"<span>{ICONS['behavior']} 交互与长连接</span>{ws_badge}</h3>"
            "<form method='post' action='/settings'>"
            + _checkbox_field("feishu_interactive", "启用 CardKit 交互式卡片",
                              inter_on, "完成态卡片附带追问表单与防误触快捷按钮")
            + _checkbox_field("feishu_ws_enabled", "启用飞书长连接监听（WSClient）",
                              ws_on, "接收 card.action.trigger 并随 Studio 保活，无需公网域名")
            + _checkbox_field("vendor_logo_enabled", "展示厂商家族 Logo 与官方模型名",
                              feige.vendor_logo_enabled(), "CardKit 自动上传并本地缓存图片 key")
            + _checkbox_field("panel_expanded", "底部统一折叠面板默认展开",
                              feige.panel_expanded_default())
            + _panel_chips_html()
            + _checkbox_field("footer_enabled", "显示底部统计脚注行",
                              feige.footer_enabled(),
                              "默认关：面板头已承载同一套统计，不再重复一行；钉钉/Telegram 静态版式不受影响")
            + _footer_chips_html()
            + f"<div id='interactive-advanced-wrap' class='cond-wrap{adv_dim}'>"
              f"<details class='fold-card' id='interactive-advanced-fold' style='margin:6px 0 12px'{adv_open}>"
              "<summary class='fold-summary'>"
              "<span class='fold-title'>权限白名单与无头续跑参数</span>"
              "<span class='fold-right'><span class='fold-badge empty'>按需配置</span><span class='fold-caret'></span></span>"
              "</summary><div class='fold-body'>"
            + (f"<div class='frow'><div class='lab'>操作人白名单（open_id / user_id） {allow_badge} "
               f"<span class='sub'>逗号或换行分隔，留空=群内成员均可操作</span></div>"
               f"<textarea name='interactive_allow_users' rows='2' placeholder='ou_xxx, ou_yyy'>{esc(allow_val)}</textarea></div>")
            + _field("resume_timeout_seconds", "无头续跑超时秒数", "300", "30~1800，默认 300")
            + feishu_guide
            + "</div></details></div>"
            + _save_btn("interactive", preview_btn=True) + "</form></div>"),
        "dingtalk": _fold_card(
            ICONS["dingtalk"], "钉钉渠道", bool(st_ch.get("dingtalk-webhook")),
            "<form method='post' action='/settings'>"
            + _field("dingtalk_webhook", "机器人 webhook")
            + _field("dingtalk_secret", "加签密钥", hint="SEC 开头，安全设置选「加签」才需要")
            + "<p class='small'>钉钉无交互卡，发 markdown 消息；需显式 <code>--channel</code> "
              "或群路由指定。</p>"
            + _save_btn("dingtalk") + "</form>"),
        "telegram": _fold_card(
            ICONS["telegram"], "Telegram 渠道", bool(st_ch.get("telegram")),
            "<form method='post' action='/settings'>"
            + _field("telegram_bot_token", "Bot token", hint="BotFather 发放，形如 123456:ABC-…")
            + _field("telegram_chat_id", "目标 chat", "-100…（群）或个人 id")
            + "<p class='small'>纯文本版式（≤4096 UTF-16 单元）；需显式渠道或路由指定。</p>"
            + _save_btn("telegram") + "</form>"),
        "llm": (
            f"<div class='card'><h3>{ICONS['llm']} 正文总结（LLM）{llm_state}</h3>"
            "<form method='post' action='/settings'>"
            + _field("llm_api_key", "API key", "OpenAI 兼容 chat/completions；配了才启用")
            + _field("llm_base_url", "Base URL", feige.DEFAULT_LLM_BASE_URL)
            + ("<div class='frow'><div class='lab'>模型 <span class='tag on' id='llm-model-badge'>"
               + (f"已配置 · {esc(key_source('llm_model'))}" if key_source("llm_model") else "默认")
               + "</span> <span class='sub'>输入或点击下拉拉取模型</span></div>"
               "<div class='preset-picker' id='llm-model-picker'>"
               f"<input type='text' id='llm_model' name='llm_model' value='{esc(cur_model)}' "
               f"placeholder='{esc(feige.DEFAULT_LLM_MODEL)}' autocomplete='off'>"
               "<button type='button' id='llm-model-toggle' class='btn tiny' title='展开/拉取模型列表' style='position:absolute;right:4px;top:4px;bottom:4px;height:auto;padding:0 8px;z-index:2;background:var(--tertiary);border-color:transparent'>▼</button>"
               "<div id='llm-model-menu' class='preset-menu hidden'>"
               "<div style='display:flex;gap:6px;margin-bottom:4px'>"
               "<input type='text' id='llm-model-filter' placeholder='搜索模型…' style='flex:1'>"
               f"<button type='button' id='llm-models-refresh' class='btn tiny' title='从当前服务地址拉取最新模型'>{ICONS['refresh']} 拉取</button>"
               "</div>"
               "<div id='llm-model-list'><div class='pt-empty'>点击「拉取」或展开菜单从服务端获取模型</div></div>"
               "</div></div></div>")
            + _field("llm_max_chars", "摘要目标字数", str(feige.LLM_MAX_CHARS), "≥50")
            + _field("llm_min_chars", "总结触发字数", str(feige.LLM_MIN_CHARS),
                     "正文超过这个字数就先浓缩成摘要（0 = 只在超渠道上限才总结）")
            + "<div class='frow'><label><input type='checkbox' name='llm_off' value='1' "
              + ("checked" if feige.load_settings().get("summarize") is False else "")
              + "> 显式关闭总结（即使配了 key 也只截断）</label></div>"
            + "<p class='small'>启用后正文超过「总结触发字数」（默认 300 字）就先由后台"
              "发送进程调 LLM 浓缩成战报摘要，不再原样镜像；未配置/失败退化为硬截断；"
              "宿主 hook 前台零网络阻塞。</p>"
            + _save_btn("llm") + "</form></div>"),
        "behavior": (
            f"<div class='card'><h3>{ICONS['behavior']} 行为开关</h3><form method='post' action='/settings'>"
            + _select_field("stats_scope", "统计口径",
                            {"turn": "单轮（只算最后一轮）",
                             "session": "全程（整个会话累计）"},
                            feige.stats_scope())
            + _field("debounce_seconds", "收尾去抖秒数", "0", "同会话 N 秒内又收尾只发最后一张；0=每轮都发")
            + _field("routes_file", "群路由文件路径", "~/.feige-routes.json", "留空用默认")
            + "<p class='small'>宿主 hook 的启用开关（hook_notify）与凭据在各自插件设置里"
              "也有入口——插件设置注入为环境变量，优先于本页。</p>"
            + _save_btn("behavior") + "</form></div>"),
    }
    body = (_banner(msg, err) + _warn_html()
            + "<div class='grid'>" + cards["feishu"] + cards["interactive"]
            + cards["llm"] + cards["dingtalk"] + cards["telegram"]
            + cards["behavior"] + "</div>")
    # 群路由不在主页重复挂卡——独立页签已有入口
    return page("配置", body, tab="config")


def _select_field(skey: str, label: str, options: dict, current: str) -> str:
    src = key_source(skey)
    env_name = _ENV_FOR_KEY.get(skey, "")
    locked = src == "环境变量"
    badge = (f"<span class='tag on'>已配置 · {esc(src)}</span>" if src
             else "<span class='tag'>默认</span>")
    opts = "".join(f"<option value='{esc(k)}' {'selected' if current == k else ''}>{esc(v)}</option>"
                   for k, v in options.items())
    dis = " disabled class='env-locked'" if locked else ""
    env_note = (f"<div class='env-note'>🔒 环境变量 <code>{esc(env_name)}</code> 生效中，此处改动不会覆盖它</div>"
                if locked and env_name else "")
    return (f"<div class='frow'><div class='lab'>{esc(label)} {badge}</div>"
            f"<select name='{skey}'{dis}>{opts}</select>{env_note}</div>")


# ── POST /settings：按卡片白名单保存 ─────────────────────────────────────────

_INTERACTIVE_KEYS = [
    "feishu_interactive", "feishu_ws_enabled", "vendor_logo_enabled",
    "panel_expanded", "panel_fields", "footer_enabled", "footer_fields",
    "interactive_allow_users", "resume_timeout_seconds",
]
_CARD_KEYS = {
    "feishu": ["feishu_webhook", "feishu_webhook_secret", "feishu_app_id",
               "feishu_app_secret", "feishu_base_url", "feishu_chat_id",
               # 按项目覆盖接收群：键名 feishu_chat_id__<项目名>（与路由标签逐字一致）
               "feishu_chat_id__10router"],
    "interactive": list(_INTERACTIVE_KEYS),
    "dingtalk": ["dingtalk_webhook", "dingtalk_secret"],
    "telegram": ["telegram_bot_token", "telegram_chat_id"],
    "llm": ["llm_api_key", "llm_base_url", "llm_model", "llm_max_chars", "llm_min_chars"],
    "behavior": ["stats_scope", "debounce_seconds", "routes_file"],
}
_BOOL_KEYS = {"feishu_interactive", "feishu_ws_enabled", "vendor_logo_enabled",
              "panel_expanded", "footer_enabled"}
_FIELD_LIST_KEYS = {"footer_fields", "panel_fields"}
_CARD_TITLES = {"feishu": "飞书渠道", "interactive": "交互与长连接", "dingtalk": "钉钉渠道",
                "telegram": "Telegram", "llm": "正文总结", "behavior": "行为开关"}


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
    keys = list(_CARD_KEYS[card])
    # 兼容旧测试/直接调用：form=feishu 且附带任意交互键时顺带更新（普通凭据卡提交不会误清开关）
    if card == "feishu" and any(k in form for k in _INTERACTIVE_KEYS):
        keys.extend(_INTERACTIVE_KEYS)
    for key in keys:
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
        if key in _BOOL_KEYS:
            settings[key] = "1" if (form.get(key) or [""])[0].strip() in ("1", "true", "on") else "0"
            continue
        if key == "resume_timeout_seconds":
            v = (form.get(key) or [""])[0].strip()
            if v:
                try:
                    num = int(float(v))
                    if not (30 <= num <= 1800):
                        return f"非法续跑超时秒数：{v}（须在 30~1800 之间）"
                    settings[key] = str(num)
                except ValueError:
                    return f"非法续跑超时秒数：{v}（须为数字）"
            else:
                settings.pop(key, None)
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
        if key == "llm_min_chars":
            v = (form.get(key) or [""])[0].strip()
            if v:
                try:
                    v = str(max(int(v), 0))
                except ValueError:
                    return f"非法总结触发字数：{v}（须为 ≥0 的整数）"
                settings[key] = v
            else:
                settings.pop(key, None)
            continue
        if key in _FIELD_LIST_KEYS:
            v = (form.get(key) or [""])[0].strip()
            if v:
                settings[key] = [x.strip() for x in v.split(",") if x.strip()]
            else:
                settings[key] = []
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
        f"<td><span class='badge {'ok' if ok else 'mute'}'>{'已配置' if ok else '未配置'}</span></td>"
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
    llm_badge = "<span class='badge ok'>已启用</span>" if llm["enabled"] else "<span class='badge mute'>未启用</span>"
    llm_info = (f"{llm_badge} · 模型 "
                f"<code>{esc(llm['model'])}</code> · base <code>{esc(llm['base_url'])}</code> · "
                f"超 {llm['min_chars']} 字触发 · 目标 ≤{llm['max_chars']} 字 · key 来源："
                f"{esc(key_source('llm_api_key') or '未配置')}")
    ws = feige_interactive.get_ws_manager().status()
    ws_badge_map = {
        "running": "<span class='badge ok'>🟢 监听中</span>",
        "connecting": "<span class='badge warn'>🟡 连接中</span>",
        "stopped": "<span class='badge mute'>⚪ 未启动</span>",
        "unavailable": "<span class='badge warn'>🟠 缺少 lark-oapi</span>",
        "error": "<span class='badge bad'>🔴 连接异常</span>",
    }
    ws_state_label = ws_badge_map.get(ws["state"], f"<span class='badge mute'>{esc(ws['state'])}</span>")
    allow_users = feige.allow_users_list()
    allow_info = f"白名单 {len(allow_users)} 人" if allow_users else "未设白名单（群内成员可操作）"
    cli_probe = feige_interactive.probe_host_clis()
    cli_rows = "".join(
        f"<tr><td>{esc(info['label'])}</td>"
        f"<td><code>{esc(info['cli'])}</code></td>"
        f"<td><span class='badge {'ok' if info['available'] else ('mute' if not info['resumable'] else 'bad')}'>"
        f"{'可用' if info['available'] else ('不支持续跑' if not info['resumable'] else '未检测到命令')}</span></td>"
        f"<td class='small'>{esc(info['note'])}</td></tr>"
        for info in cli_probe.values()
    )
    sess_count = len(feige_interactive.list_sessions())
    ws_err_html = f"<p class='err'>{esc(ws['error'])}</p>" if ws.get("error") else ""
    ws_card = (
        f"<div class='card'><h3>交互式卡片 & 长连接回调</h3>"
        f"<p>长连接状态：<b>{ws_state_label}</b> · 开关：<code>{'开' if ws['enabled'] else '关'}</code> · "
        f"SDK（<code>lark-oapi</code>）：<code>{'已安装' if ws['available'] else '未安装'}</code> · "
        f"已处理回调：<code>{ws['events_handled']}</code> · 活跃会话上下文：<code>{sess_count}</code> · "
        f"{esc(allow_info)}</p>"
        f"{ws_err_html}"
        f"<form method='post' action='/ws/sync' style='margin:6px 0 10px'>"
        f"<button type='submit'>{ICONS['refresh']} 同步长连接状态</button></form>"
        f"<table><tr><th>宿主</th><th>CLI</th><th>续跑能力</th><th>说明</th></tr>{cli_rows}</table></div>"
    )
    banner = f'<div class="msg">{esc(msg)}</div>' if msg else ""
    body = (f"{banner}<div class='card'><h3>渠道凭据</h3>"
            f"<table><tr><th>渠道</th><th>凭据</th><th>测试</th></tr>{rows}</table>"
            f"<p class='small'>凭据只显示配置与否（环境变量或设置文件），密钥值与 webhook "
            f"内容永不回显。配置请去「配置」页。</p></div>"
            f"{ws_card}"
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

def _auto_projects() -> set[str]:
    """设置文件里已开启自动战报的项目名（auto_notify_projects 字符串数组）。"""
    raw = feige.load_settings().get("auto_notify_projects")
    return {str(p).strip() for p in raw if str(p).strip()} if isinstance(raw, list) else set()


def _routes_form(doc: dict, msg: str = "", err: str = "") -> bytes:
    routes = (doc or {}).get("routes", {})
    auto_on = _auto_projects()
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
        checked = " checked" if proj in auto_on else ""
        auto_tip = ("该项目每轮收尾自动发战报" if proj in auto_on
                    else "不勾 = 只保留路由，不自动发（可在会话里手动 --route）")
        blocks.append(
            f"<div class='card'><h3>项目 <input type='text' name='proj_{i}' value='{esc(proj)}' "
            f"style='width:280px' oninput=\"syncAuto(this)\">"
            f"<label class='small' style='margin-left:12px' title=\"{esc(auto_tip)}\">"
            f"<input type='checkbox' name='auto_{esc(proj)}' value='1'{checked}> 自动战报</label></h3>"
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
// 项目名输入即同步「自动战报」复选框的 name，保存后新项目立刻能被勾上
function syncAuto(el){const box=el.parentNode.querySelector("input[type=checkbox]");
 if(box) box.name="auto_"+el.value.trim();}
function addProject(){const f=document.getElementById("rt");const i=window._n++;
 f.insertAdjacentHTML("beforeend",`<div class="card"><h3>项目 <input type="text" name="proj_${i}" style="width:280px" oninput="syncAuto(this)">`+
 `<label class="small" style="margin-left:12px"><input type="checkbox" name="auto_" value="1"> 自动战报</label></h3>`+
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


def _save_auto_projects(names: set[str]) -> tuple[set[str], str]:
    """把「自动战报」勾选结果写进设置文件的 auto_notify_projects（字符串数组）。

    只动这一个键：路由表单一个 POST 提交，其它设置项原样保留。
    失败返回 (原值, 错误信息)，调用方继续保存路由（开关失败不该连累路由）。
    """
    settings = dict(feige.load_settings())
    current = settings.get("auto_notify_projects")
    keep = sorted({str(p).strip() for p in current if str(p).strip()}) if isinstance(current, list) else []
    wanted = sorted(names)
    if keep == wanted:
        return set(wanted), ""
    settings["auto_notify_projects"] = wanted
    path = Path(feige.settings_file_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            shutil.copy2(path, str(path) + ".bak")
        tmp = str(path) + ".tmp"
        Path(tmp).write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return set(keep), f"自动开关保存失败：{feige.redact(exc)}"
    return set(wanted), ""


def handle_routes_post(body: bytes) -> bytes:
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    doc, err = _parse_routes_form(form)
    if err:
        return _routes_form(feige.load_routes(), err=err)
    # 自动战报开关：勾了就写进设置文件（per-project），取消勾选即删除
    auto_on = {name for name in doc["routes"]
               if (form.get(f"auto_{name}") or [""])[0].strip() in ("on", "1", "true")}
    saved_auto, auto_err = _save_auto_projects(auto_on)
    # 冒烟：保存后语义与 feige.resolve_routes 一致，先 dry 一遍计数
    def _smoke(p: str) -> str:
        targets = feige.resolve_routes(p, doc)
        bad = sum(1 for t in targets if t.get("_unresolved"))
        flag = "自动发" if p in auto_on else "仅手动"
        return f"{p}: {len(targets)} 目标 · {flag}" + (
            f"（{bad} 个引用了未设置的 $ENV，发送时将跳过）" if bad else "")
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
    note = (f"已保存（冒烟：{smoke or '空路由'}），原文件备份为 .bak"
            + (f"；自动开启：{', '.join(sorted(saved_auto))}" if saved_auto else "；未开启自动战报"))
    return _routes_form(doc, msg=note, err=auto_err)


# ── 页面：卡片预览（对齐 fry-cards Studio CardKit v2.0 真实渲染器） ────────────

# 外观示意的状态条配色：ok/error 跟随主题 token，running 用家族蓝（飞书卡蓝头）
_STATUS_COLOR = {"running": "#3b82f6", "ok": "var(--ok)", "error": "var(--bad)"}

_SAMPLE_MODELS = [
    ("claude-sonnet-4", "Claude sonnet-4 · Anthropic"),
    ("tencent/hy4-flash:free", "Hy4 flash · 腾讯混元"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 · DeepSeek"),
    ("kimi-k3", "Kimi K3 · Moonshot"),
    ("longcat/LongCat-2.5-Preview", "LongCat 2.5 · 美团"),
    ("glm-5.3-flashx", "GLM 5.3 · 智谱"),
    ("qwen4-max", "Qwen4 Max · 阿里云"),
    ("mimo-v2.6-flash-free", "MiMo V2.6 · 小米"),
]

_PREVIEW_PRESETS = [
    {
        "id": "ok_interactive",
        "label": "✅ 完成+追问",
        "title": "重构飞鸽 Studio 并升级 v2.0.0",
        "channel": "feishu-cardkit",
        "host": "claude-code",
        "status": "ok",
        "body": "**核心进展**：对齐 fry-cards Studio 真实预览器与可折叠配置体验。\n- 预览页支持 **CardKit v2.0** 仿真与场景预设\n- 全部离线自动化测试通过",
        "detail": "详细执行摘要：\n1. 新增 POST /api/preview 实时返回真实 CardKit 2.0 结构；\n2. 前端 FeigePreview 还原折叠面板、厂商 Logo、Markdown 表格与防误触按钮。\n\n| 模块 | 状态 |\n| --- | --- |\n| CardKit Builder | 已对齐 |\n| Studio UI | v2.0.0 |",
        "project": "feige-fry-cards",
        "model": "claude-sonnet-4",
        "thinking": "3",
        "tools": "14",
        "context": "42%",
        "elapsed": "2m18s",
        "tool_summary": "Read ×4 · Edit ×3 · Run ×2",
    },
    {
        "id": "error_report",
        "label": "❌ 报错+红框",
        "title": "集成测试执行中断",
        "channel": "feishu-cardkit",
        "host": "codex",
        "status": "error",
        "body": "**失败原因**：依赖服务连接超时（`127.0.0.1:8787` 未就绪）。\n请检查本地守护进程状态后点击下方按钮续跑。",
        "detail": "stderr 摘要：\nConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。\n下一步建议：先执行 python webui.py start",
        "project": "10router",
        "model": "deepseek-v4.1-flash",
        "thinking": "2",
        "tools": "6",
        "context": "68%",
        "elapsed": "45s",
        "tool_summary": "Run ×3 · Read ×3",
    },
    {
        "id": "running",
        "label": "🔵 流式运行中",
        "title": "正在执行代码审查与打包…",
        "channel": "feishu-cardkit",
        "host": "claude-code",
        "status": "running",
        "body": "正在运行全量单元测试与风格核验，完成后自动更新为终态战报。",
        "detail": "",
        "project": "feige-fry-cards",
        "model": "kimi-k3",
        "thinking": "1",
        "tools": "5",
        "context": "24%",
        "elapsed": "18s",
        "tool_summary": "Read ×5",
    },
    {
        "id": "long_detail",
        "label": "📜 长详情+进度条",
        "title": "多代理流水线汇总完成",
        "channel": "feishu-cardkit",
        "host": "zcode",
        "status": "ok",
        "body": "**总结**：完成 4 个模块改造并核验 180+ 测试用例。",
        "detail": "#### 工具与产物明细\n- `feige.py`：新增 20 套厂商 Logo 映射与 `unified_panel`\n- `feige_interactive.py`：接入 WSClient `card.action.trigger`\n- `webui.py`：升级折叠配置卡与 CardKit 2.0 实时预览",
        "project": "hermes-fry-cards",
        "model": "tencent/hy4-flash:free",
        "thinking": "6",
        "tools": "28",
        "context": "153.6k/200.0k (77%)",
        "elapsed": "5m04s",
        "tool_summary": "Read ×12 · Edit ×10 · Bash ×6",
    },
    {
        "id": "antigravity",
        "label": "🕊️ 不可续跑宿主",
        "title": "Antigravity 会话收尾",
        "channel": "feishu-cardkit",
        "host": "antigravity",
        "status": "ok",
        "body": "**一句话战报**：完成方案调研与文档梳理。",
        "detail": "本会话由 Antigravity CLI 运行完成；该宿主暂不支持无头续跑，卡片自动展示降级说明。",
        "project": "docs-site",
        "model": "longcat/LongCat-2.5-Preview",
        "thinking": "2",
        "tools": "7",
        "context": "31%",
        "elapsed": "1m12s",
        "tool_summary": "Read ×7",
    },
    {
        "id": "webhook_static",
        "label": "🛰️ Webhook 降级",
        "title": "静态群机器人战报",
        "channel": "feishu-webhook",
        "host": "claude-code",
        "status": "ok",
        "body": "**静态渠道兼容**：不发追问表单，折叠面板无自定义图标、保持干净纯标题。",
        "detail": "feishu-webhook 渠道使用自定义机器人签名校验，不依赖 App 凭据。",
        "project": "feige-fry-cards",
        "model": "glm-5.3-flashx",
        "thinking": "1",
        "tools": "4",
        "context": "18%",
        "elapsed": "32s",
        "tool_summary": "Edit ×2 · Read ×2",
    },
]


def _safe_int(val, default: int = 0) -> int:
    try:
        return max(0, int(float(str(val).strip())))
    except (ValueError, TypeError):
        return default


def build_preview_payload(params: dict) -> dict:
    """用真实 feige.send_report(..., dry_run=True) 生成预览载荷与厂商资产元数据。"""
    def p(key: str, default: str = "") -> str:
        val = params.get(key)
        if isinstance(val, list):
            val = val[0] if val else default
        return str(default if val is None else val)

    status = p("status", "ok").strip()
    if status not in feige.STATUS_TEMPLATE:
        status = "ok"
    ch = p("channel", "").strip()
    if ch and ch not in feige.CHANNELS:
        ch = ""
    effective_ch = ch or feige.pick_channel() or "feishu-webhook"
    host = p("host", "claude-code" if effective_ch == "feishu-cardkit" else "").strip()
    title = p("title", "任务完成") or "标题"
    body = p("body", "**一句话战报**")
    detail_txt = p("detail", "")
    tool_sum = p("tool_summary", "")
    model = p("model", "claude-sonnet-4")
    stats = {
        "project": p("project", ""),
        "model": model,
        "thinking": _safe_int(p("thinking", "0")),
        "tools": _safe_int(p("tools", "0")),
        "context": p("context", ""),
        "elapsed": p("elapsed", ""),
    }
    ok, raw_detail = feige.send_report(
        title, body, stats=stats, status=status, channel=ch, dry_run=True,
        host=host, detail_body=detail_txt, tool_summary=tool_sum,
    )
    payload_obj = None
    card_obj = None
    if ok:
        try:
            payload_obj = json.loads(raw_detail)
            if isinstance(payload_obj, dict):
                card_obj = payload_obj.get("card") if isinstance(payload_obj.get("card"), dict) else payload_obj
        except ValueError:
            payload_obj = {"raw": raw_detail}
    logo_file = feige.resolve_vendor_logo_asset(model) if feige.vendor_logo_enabled() else None
    logo_url = f"/assets/vendor-logos/{logo_file}" if logo_file else ""
    official_model = feige.resolve_vendor_display_name(model) if model else ""
    return {
        "ok": bool(ok),
        "channel": effective_ch,
        "status": status,
        "raw_json": raw_detail,
        "payload": payload_obj,
        "card": card_obj,
        "logo_url": logo_url,
        "official_model": official_model,
        "host": host,
        "host_label": feige_interactive.host_label(host) if host else "",
        "resumable": bool(host and feige_interactive.is_host_resumable(host)),
    }


_PREVIEW_JS = r"""
<script>
(function(){
  var PRESETS = __PRESETS_JSON__;
  var form = document.getElementById('pv-form');
  var stage = document.getElementById('pv-stage');
  var jsonBox = document.getElementById('pv-json-out');
  var metaEl = document.getElementById('pv-meta');
  var toastEl = document.getElementById('pv-toast');
  var modelSel = document.getElementById('pv-model-presets');
  var ssrWrap = document.getElementById('ssr-preview-wrap');
  if(!form || !stage) return;

  var toastTimer = null;
  function showToast(msg){
    if(!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function(){ toastEl.classList.remove('show'); }, 2600);
  }
  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(ch){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch];
    });
  }
  var FONT_COLORS = {
    red:'#dc2626', green:'#16a34a', blue:'#1456f0', orange:'#d97706',
    grey:'#646a73', gray:'#646a73', purple:'#7c3aed', turquoise:'#0d9488'
  };
  function inlineMd(text){
    var s = esc(text);
    // 放行安全飞书 font color 语法（先提取占位再恢复）
    s = s.replace(/&lt;font\s+color=(?:&#39;|&quot;)?([a-zA-Z]+)(?:&#39;|&quot;)?&gt;([\s\S]*?)&lt;\/font&gt;/g,
      function(_, col, inner){
        var c = FONT_COLORS[col.toLowerCase()];
        return c ? ('<span style="color:' + c + '">' + inner + '</span>') : inner;
      });
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return s;
  }
  function renderMarkdown(raw){
    var text = String(raw == null ? '' : raw).replace(/\r\n/g, '\n');
    if(!text.trim()) return '';
    var lines = text.split('\n');
    var out = [], i = 0;
    while(i < lines.length){
      var ln = lines[i];
      if(/^```/.test(ln.trim())){
        var code = []; i++;
        while(i < lines.length && !/^```/.test(lines[i].trim())){ code.push(lines[i]); i++; }
        if(i < lines.length) i++;
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      if(ln.trim() === '---' || ln.trim() === '***'){
        out.push('<hr class="fc-hr">'); i++; continue;
      }
      if(/^#{1,5}\s+/.test(ln)){
        var lvl = /^#{1,3}\s/.test(ln) ? 'h4' : 'h5';
        out.push('<' + lvl + '>' + inlineMd(ln.replace(/^#+\s+/, '')) + '</' + lvl + '>');
        i++; continue;
      }
      // Markdown 表格
      if(ln.indexOf('|') !== -1 && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i+1])){
        var hdr = ln.trim().replace(/^\||\|$/g, '').split('|');
        i += 2;
        var tbl = '<table><thead><tr>' + hdr.map(function(h){ return '<th>' + inlineMd(h.trim()) + '</th>'; }).join('') + '</tr></thead><tbody>';
        while(i < lines.length && lines[i].indexOf('|') !== -1 && lines[i].trim()){
          var row = lines[i].trim().replace(/^\||\|$/g, '').split('|');
          tbl += '<tr>' + row.map(function(c){ return '<td>' + inlineMd(c.trim()) + '</td>'; }).join('') + '</tr>';
          i++;
        }
        tbl += '</tbody></table>';
        out.push(tbl); continue;
      }
      if(/^[-*]\s+/.test(ln)){
        var ul = [];
        while(i < lines.length && /^[-*]\s+/.test(lines[i])){
          ul.push('<li>' + inlineMd(lines[i].replace(/^[-*]\s+/, '')) + '</li>'); i++;
        }
        out.push('<ul>' + ul.join('') + '</ul>'); continue;
      }
      if(/^\d+\.\s+/.test(ln)){
        var ol = [];
        while(i < lines.length && /^\d+\.\s+/.test(lines[i])){
          ol.push('<li>' + inlineMd(lines[i].replace(/^\d+\.\s+/, '')) + '</li>'); i++;
        }
        out.push('<ol>' + ol.join('') + '</ol>'); continue;
      }
      if(/^>\s?/.test(ln)){
        var bq = [];
        while(i < lines.length && /^>\s?/.test(lines[i])){
          bq.push(inlineMd(lines[i].replace(/^>\s?/, ''))); i++;
        }
        out.push('<blockquote>' + bq.join('<br>') + '</blockquote>'); continue;
      }
      if(!ln.trim()){ i++; continue; }
      var para = [];
      while(i < lines.length && lines[i].trim() && !/^(```|#{1,5}\s|[-*]\s|\d+\.\s|>\s?|---$|\*\*\*$)/.test(lines[i])){
        para.push(inlineMd(lines[i])); i++;
      }
      out.push('<p>' + para.join('<br>') + '</p>');
    }
    return out.join('');
  }

  function renderElements(elems, res){
    if(!Array.isArray(elems)) return '';
    return elems.map(function(el){
      if(!el || typeof el !== 'object') return '';
      var tag = el.tag;
      if(tag === 'hr') return '<hr class="fc-hr">';
      if(tag === 'markdown'){
        var cls = el.text_size === 'notation' ? 'fc-notation' : 'fc-md';
        return '<div class="' + cls + '">' + renderMarkdown(el.content || '') + '</div>';
      }
      if(tag === 'collapsible_panel'){
        var bc = (el.border && el.border.color) || 'green';
        var bcls = bc === 'red' ? 'b-red' : (bc === 'blue' ? 'b-blue' : 'b-green');
        var hdr = el.header || {};
        var htitle = (hdr.title && hdr.title.content) || '';
        var icon = hdr.icon || {};
        var iconHtml = '';
        if(res.logo_url && (icon.tag === 'custom_icon' || el.element_id === 'unified_panel')){
          iconHtml = '<img class="vlogo" src="' + esc(res.logo_url) + '" alt="">';
        }
        var openAttr = el.expanded ? ' open' : '';
        return '<details class="pv-panel ' + bcls + '"' + openAttr + '>'
          + '<summary><span class="fc-sum-left">' + iconHtml + '<span>' + inlineMd(htitle) + '</span></span>'
          + '<span class="fc-sum-arrow">›</span></summary>'
          + '<div class="fc-panel-body">' + renderElements(el.elements || [], res) + '</div></details>';
      }
      if(tag === 'input'){
        var ph = (el.placeholder && el.placeholder.content) || '输入指令…';
        return '<input class="fc-input" type="text" placeholder="' + esc(ph) + '">';
      }
      if(tag === 'button'){
        var btxt = (el.text && el.text.content) || '按钮';
        var bcls = (el.type === 'primary' || el.type === 'primary_filled') ? 'fc-btn primary' : 'fc-btn';
        var ctag = el.confirm ? '<span class="fc-confirm-tag" title="带原生二次确认">🛡</span>' : '';
        var ctitle = (el.confirm && el.confirm.title && el.confirm.title.content) || '';
        var cbody = (el.confirm && el.confirm.text && el.confirm.text.content) || '';
        return '<button type="button" class="' + bcls + '" data-sim-btn="1" data-confirm-title="'
          + esc(ctitle) + '" data-confirm-body="' + esc(cbody) + '">' + esc(btxt) + ctag + '</button>';
      }
      if(tag === 'form'){
        return '<div class="fc-form">' + renderElements(el.elements || [], res) + '</div>';
      }
      if(tag === 'column_set'){
        var cols = (el.columns || []).map(function(c){ return renderElements(c.elements || [], res); }).join('');
        return '<div class="fc-actions">' + cols + '</div>';
      }
      if(tag === 'action'){
        return '<div class="fc-actions">' + renderElements(el.actions || [], res) + '</div>';
      }
      return '';
    }).join('');
  }

  function renderCard(res){
    if(!res || !res.ok){
      stage.innerHTML = '<div class="err">' + esc((res && res.raw_json) || '预览生成失败') + '</div>';
      return;
    }
    var card = res.card;
    if(card && (card.schema === '2.0' || (card.body && Array.isArray(card.body.elements)))){
      var hdr = card.header || {};
      var tmpl = hdr.template || 'blue';
      var tmap = {blue:'t-blue', green:'t-green', red:'t-red', grey:'t-grey', carmine:'t-red'};
      var hcls = tmap[tmpl] || 't-blue';
      var htitle = (hdr.title && hdr.title.content) || '';
      var elems = (card.body && card.body.elements) || [];
      stage.innerHTML =
        '<div class="feishu-card">'
        + '<div class="fc-header ' + hcls + '">' + esc(htitle) + '</div>'
        + '<div class="fc-body">' + renderElements(elems, res) + '</div>'
        + '</div>';
    }else if(card && card.msgtype === 'markdown' && card.markdown){
      stage.innerHTML =
        '<div class="feishu-card">'
        + '<div class="fc-header t-blue">钉钉 Markdown 消息</div>'
        + '<div class="fc-body"><div class="fc-md">' + renderMarkdown(card.markdown.text || '') + '</div></div>'
        + '</div>';
    }else{
      var rawText = (res.payload && res.payload.text) || res.raw_json || '';
      stage.innerHTML =
        '<div class="feishu-card">'
        + '<div class="fc-header t-grey">' + esc(res.channel || '文本消息') + '</div>'
        + '<div class="fc-body"><pre style="white-space:pre-wrap;margin:0">' + esc(rawText) + '</pre></div>'
        + '</div>';
    }
  }

  function readFormData(){
    var fd = new FormData(form);
    var out = {};
    fd.forEach(function(v, k){ if(k !== 'go') out[k] = v; });
    return out;
  }

  var reqSeq = 0;
  function refreshPreview(){
    var mySeq = ++reqSeq;
    var payload = readFormData();
    fetch('/api/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function(r){ return r.json(); }).then(function(res){
      if(mySeq !== reqSeq) return;
      renderCard(res);
      if(jsonBox) jsonBox.textContent = res.raw_json || '';
      if(metaEl){
        var parts = ['渠道 ' + (res.channel || '-')];
        if(res.official_model) parts.push('官方模型名 ' + res.official_model);
        if(res.host_label) parts.push('宿主 ' + res.host_label + (res.resumable ? '（支持无头续跑）' : '（不可续跑降级）'));
        metaEl.textContent = parts.join(' · ');
      }
      if(ssrWrap) ssrWrap.classList.add('hidden');
    }).catch(function(err){
      if(mySeq !== reqSeq) return;
      stage.innerHTML = '<div class="err">预览请求失败：' + esc(err && err.message || err) + '</div>';
    });
  }

  var timer = null;
  function scheduleRefresh(){
    clearTimeout(timer);
    timer = setTimeout(refreshPreview, 200);
  }

  // 预设切换
  document.querySelectorAll('.preset-pill[data-preset]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var pid = btn.getAttribute('data-preset');
      var item = PRESETS.find(function(x){ return x.id === pid; });
      if(!item) return;
      document.querySelectorAll('.preset-pill[data-preset]').forEach(function(b){ b.classList.remove('on'); });
      btn.classList.add('on');
      ['title','channel','host','status','body','detail','project','model','thinking','tools','context','elapsed','tool_summary'].forEach(function(k){
        var el = form.elements[k];
        if(el && k in item) el.value = item[k];
      });
      if(modelSel) modelSel.value = item.model || '';
      refreshPreview();
      showToast('已切换预设：' + item.label);
    });
  });

  if(modelSel){
    modelSel.addEventListener('change', function(){
      if(modelSel.value && form.elements['model']){
        form.elements['model'].value = modelSel.value;
        refreshPreview();
      }
    });
  }

  form.addEventListener('input', scheduleRefresh);
  form.addEventListener('change', scheduleRefresh);
  form.addEventListener('submit', function(e){
    e.preventDefault();
    refreshPreview();
  });

  // 模拟飞书卡片按钮点击（二次确认弹窗演练）
  stage.addEventListener('click', function(e){
    var btn = e.target.closest('button[data-sim-btn]');
    if(!btn) return;
    var ct = btn.getAttribute('data-confirm-title');
    var cb = btn.getAttribute('data-confirm-body');
    if(ct){
      if(window.confirm(ct + '\n\n' + cb)){
        showToast('已模拟触发防误触回调：「' + btn.textContent.trim() + '」');
      }
    }else if(btn.textContent.indexOf('追问') !== -1){
      var inp = stage.querySelector('.fc-input');
      var txt = inp ? inp.value.trim() : '';
      showToast(txt ? ('已模拟提交追问：「' + txt + '」') : '请先在卡片输入框中填写追问内容');
    }else{
      showToast('已模拟点击：「' + btn.textContent.trim() + '」');
    }
  });

  if(window.location.search.indexOf('saved=1') !== -1){
    showToast('✅ 设置已保存，预览已按最新配置渲染');
  }
  refreshPreview();
})();
</script>
"""


def page_preview(q: dict) -> bytes:
    def gv(key: str, default: str = "") -> str:
        return (q.get(key) or [default])[0]

    has_go = bool(gv("go"))
    cur_channel = gv("channel", "")
    cur_host = gv("host", "claude-code")
    cur_status = gv("status", "ok")
    cur_title = gv("title", "任务完成")
    cur_body = gv("body", "**一句话战报**")
    cur_detail = gv("detail", "")
    cur_project = gv("project", "")
    cur_model = gv("model", "claude-sonnet-4")
    cur_thinking = gv("thinking", "")
    cur_tools = gv("tools", "")
    cur_context = gv("context", "")
    cur_elapsed = gv("elapsed", "")
    cur_tool_sum = gv("tool_summary", "")

    ssr_html = ""
    ssr_json = ""
    if has_go:
        pv = build_preview_payload({
            "title": cur_title, "body": cur_body, "status": cur_status,
            "channel": cur_channel, "host": cur_host, "detail": cur_detail,
            "tool_summary": cur_tool_sum, "project": cur_project, "model": cur_model,
            "thinking": cur_thinking, "tools": cur_tools, "context": cur_context,
            "elapsed": cur_elapsed,
        })
        stats = {
            "project": cur_project, "model": cur_model,
            "thinking": _safe_int(cur_thinking), "tools": _safe_int(cur_tools),
            "context": cur_context, "elapsed": cur_elapsed,
        }
        status = pv["status"]
        ch = cur_channel
        host = pv["host"]
        footer = feige.stats_footer(stats)
        logo_url = pv["logo_url"]
        logo_html = f"<img class='vlogo' src='{esc(logo_url)}' alt=''>" if logo_url else ""
        panel_title = feige._build_panel_header_text(stats, logo_image_key="preview" if logo_url else "")
        panel_html = ""
        if status != "running" and (stats.get("model") or stats.get("project") or cur_detail or cur_tool_sum or host):
            det_parts = []
            if cur_detail:
                det_parts.append(f"<pre style='white-space:pre-wrap;margin:6px 0'>{esc(cur_detail)}</pre>")
            if cur_tool_sum:
                det_parts.append(f"<div class='small'>🔧 工具调用：{esc(cur_tool_sum)}</div>")
            if host:
                det_parts.append(f"<div class='small'>宿主：{esc(feige_interactive.host_label(host))} · 上下文：<code>#preview</code></div>")
            panel_open = " open" if feige.panel_expanded_default() else ""
            panel_html = (
                f"<details class='pv-panel b-{esc(feige.PANEL_BORDER_COLOR.get(status, 'green'))}'{panel_open}>"
                f"<summary>{logo_html}<span>{esc(panel_title)}</span></summary>"
                f"<div style='margin-top:6px'>{''.join(det_parts) or '<span class=small>暂无更多详情</span>'}</div></details>"
            )
        interactive_html = ""
        if ch == "feishu-cardkit" and status != "running" and feige.interactive_enabled() and host:
            if feige_interactive.is_host_resumable(host):
                interactive_html = (
                    "<div class='pv-form'>"
                    "<input type='text' disabled placeholder='输入追问或继续指令…' style='margin-bottom:6px'>"
                    "<div class='pv-btns'>"
                    "<button type='button' class='primary' disabled>📤 发送追问</button>"
                    "<button type='button' disabled>▶️ 继续执行</button>"
                    "<button type='button' disabled>🧪 跑测试核验</button>"
                    "</div></div>"
                )
            else:
                interactive_html = (
                    f"<div class='pv-form'><button type='button' disabled>ℹ️ 续跑说明</button>"
                    f"<div class='small' style='margin-top:4px'>💡 {esc(feige_interactive.host_label(host))} 暂不支持远程无头续跑</div></div>"
                )
        mock = (
            f"<div class='card' style='padding:0;overflow:hidden;max-width:460px'>"
            f"<div class='bar' style='background:{_STATUS_COLOR[status]}'></div>"
            f"<div style='padding:12px'><b>{feige.STATUS_PREFIX[status]} {esc(cur_title)}</b>"
            f"<pre style='white-space:pre-wrap;border:none;background:none;padding:6px 0'>{esc(cur_body)}</pre>"
            f"{panel_html}{interactive_html}"
            + (f"<hr><p class='small'>{esc(footer)}</p>"
               if status != "running" and footer and feige.footer_enabled() else "")
            + "</div></div>")
        ssr_json = pv["raw_json"]
        ssr_html = (
            f"<div id='ssr-preview-wrap'>"
            f"<div class='card'><h3>外观示意（SSR 基线）</h3>{mock}</div>"
            f"<div class='card'><h3>dry-run 载荷 JSON</h3>"
            f"{'<pre>' + esc(ssr_json) + '</pre>' if pv['ok'] else '<div class=err>' + esc(ssr_json) + '</div>'}</div>"
            f"</div>"
        )

    opts = "".join(f"<option {'selected' if cur_channel == c else ''}>{c}</option>"
                   for c in feige.CHANNELS)
    sopts = "".join(f"<option {'selected' if cur_status == s else ''}>{s}</option>"
                    for s in ("ok", "error", "running"))
    host_choices = [("", "(无/静态)"), ("claude-code", "Claude Code"), ("codex", "Codex"),
                    ("zcode", "ZCode"), ("kimi-code", "kimi-code"), ("antigravity", "Antigravity")]
    hopts = "".join(f"<option value='{esc(k)}' {'selected' if cur_host == k else ''}>{esc(v)}</option>"
                    for k, v in host_choices)
    mopts = "<option value=''>速选官方示例模型…</option>" + "".join(
        f"<option value='{esc(mid)}' {'selected' if cur_model == mid else ''}>{esc(mlabel)}</option>"
        for mid, mlabel in _SAMPLE_MODELS
    )
    preset_btns = "".join(
        f"<button type='button' class='preset-pill' data-preset='{esc(pr['id'])}'>{esc(pr['label'])}</button>"
        for pr in _PREVIEW_PRESETS
    )

    left_card = (
        "<div class='card'><h3>🎛️ 卡片参数 & 场景预设</h3>"
        f"<div class='preset-pills'>{preset_btns}</div>"
        "<form method='get' action='/preview' id='pv-form'>"
        "<input type='hidden' name='go' value='1'>"
        f"<p>标题 <input type='text' name='title' value='{esc(cur_title)}'></p>"
        f"<p style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<span style='flex:1 1 130px'>渠道<br><select name='channel' style='width:100%'>{opts}</select></span>"
        f"<span style='flex:1 1 120px'>宿主<br><select name='host' style='width:100%'>{hopts}</select></span>"
        f"<span style='flex:0 1 90px'>状态<br><select name='status' style='width:100%'>{sopts}</select></span></p>"
        f"<p>战报摘要正文<br><textarea name='body' rows='3'>{esc(cur_body)}</textarea></p>"
        f"<p>折叠面板详细正文（可选，支持表格/代码块）<br><textarea name='detail' rows='3' placeholder='展开折叠面板可见的完整内容…'>{esc(cur_detail)}</textarea></p>"
        f"<p style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<span style='flex:1 1 130px'>项目<br><input type='text' name='project' value='{esc(cur_project)}'></span>"
        f"<span style='flex:1 1 160px'>模型 ID<br><input type='text' name='model' value='{esc(cur_model)}'></span></p>"
        f"<p>厂商家族官方模型速选（验证 Logo 与官方名映射）<br><select id='pv-model-presets' style='width:100%'>{mopts}</select></p>"
        f"<p style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<span style='flex:0 1 70px'>💭<br><input type='text' name='thinking' value='{esc(cur_thinking)}'></span>"
        f"<span style='flex:0 1 70px'>🔧<br><input type='text' name='tools' value='{esc(cur_tools)}'></span>"
        f"<span style='flex:1 1 120px'>上下文<br><input type='text' name='context' value='{esc(cur_context)}'></span>"
        f"<span style='flex:0 1 85px'>⏱️<br><input type='text' name='elapsed' value='{esc(cur_elapsed)}'></span></p>"
        f"<p>工具摘要 <input type='text' name='tool_summary' value='{esc(cur_tool_sum)}' placeholder='Read ×2 · Edit'></p>"
        "<p style='display:flex;gap:8px;align-items:center'>"
        "<button class='primary' type='submit'>预览</button>"
        "<span class='small'>改动表单自动实时刷新 CardKit v2.0 仿真效果</span></p>"
        "</form></div>"
    )
    right_col = (
        "<div style='display:flex;flex-direction:column;gap:14px'>"
        "<div class='card'><h3>👁️ CardKit v2.0 真实卡片预览</h3>"
        "<div id='pv-meta' class='small' style='margin-bottom:8px'>正在加载实时渲染…</div>"
        "<div class='pv-stage-wrap'><div id='pv-stage'><span class='small'>等待渲染…</span></div></div>"
        "<p class='small' style='margin:8px 0 0'>提示：可直接点击折叠面板展开/收起，或点击交互按钮演练原生二次确认弹窗。</p>"
        "</div>"
        "<div class='card'><details class='fold-card'>"
        "<summary class='fold-summary'><span class='fold-title'>&lt;/&gt; 真实 dry-run 载荷 JSON</span>"
        "<span class='fold-right'><span class='fold-badge empty'>可折叠</span><span class='fold-caret'></span></span></summary>"
        f"<div class='fold-body'><pre id='pv-json-out'>{esc(ssr_json)}</pre></div>"
        "</details></div>"
        f"{ssr_html}</div>"
    )
    presets_json = json.dumps(_PREVIEW_PRESETS, ensure_ascii=False).replace("</", "<\\/")
    script = _PREVIEW_JS.replace("__PRESETS_JSON__", presets_json)
    return page("预览", f"<div class='pv-grid'>{left_card}{right_col}</div><div id='pv-toast' class='pv-toast'></div>{script}", tab="preview")


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


def _read_feige_version(root: Path) -> str:
    """从插件目录读取 feige.py 的 __version__；无 feige.py 时返回空串。"""
    txt = _read(root / "feige.py")
    if not txt:
        return ""
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', txt)
    return m.group(1) if m else "?"


def inspect_adapter_caches(home: Path | None = None) -> list[dict]:
    """检测本机各宿主与代理缓存中 feige 插件的实际路径与版本（只读）。"""
    home = home or Path.home()
    cur_ver = feige.__version__
    repo_norm = str(REPO).replace("\\", "/")
    findings: list[dict] = []

    # 1. Claude Code ~/.claude/settings.json Stop hook
    claude_settings = home / ".claude" / "settings.json"
    if claude_settings.is_file():
        s_txt = _read(claude_settings)
        cmds: list[str] = []
        try:
            s_doc = json.loads(s_txt)
            for grp in (s_doc.get("hooks") or {}).get("Stop") or []:
                for hk in grp.get("hooks") or []:
                    cmd = str(hk.get("command") or "")
                    if "feige-fry-cards" in cmd and ("stop.py" in cmd or "stop_notify.py" in cmd):
                        cmds.append(cmd)
        except Exception:
            pass
        if cmds:
            repo_stop = str((REPO / "hooks" / "stop.py")).replace("\\", "/").lower()
            points_repo = all(repo_stop in c.replace("\\", "/").lower() for c in cmds)
            ver = cur_ver if points_repo else "旧路径"
            for c in cmds:
                m = re.search(r'/feige-fry-cards/([0-9]+\.[0-9]+\.[0-9]+)/', c.replace("\\", "/"))
                if m and not points_repo:
                    ver = m.group(1)
            findings.append({
                "host": "Claude Code Stop Hook",
                "path": cmds[0],
                "version": ver,
                "stale": not points_repo,
                "note": "直接指向当前源码仓库 hooks/stop.py" if points_repo else "硬编码指向旧插件缓存（需切回当前仓库 hooks/stop.py）",
            })

    # 2. 常见插件缓存 & 市场副本目录
    cache_roots = [
        ("Codex 插件缓存", home / ".codex" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards"),
        ("Mirasim Codex Relay 缓存", home / ".mirasim" / "agent-homes" / "codex-relay" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards"),
        ("ZCode 插件缓存", home / ".zcode" / "cli" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards"),
    ]
    for label, base in cache_roots:
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / "feige.py").is_file():
                    ver = _read_feige_version(child)
                    findings.append({
                        "host": f"{label} ({child.name})",
                        "path": str(child),
                        "version": ver,
                        "stale": ver != cur_ver,
                        "note": f"已对齐 v{cur_ver}" if ver == cur_ver else f"缓存文件仍为 v{ver}（当前仓库 v{cur_ver}）",
                    })

    z_mkt = home / ".zcode" / "cli" / "plugins" / "marketplaces" / "feige-fry-cards"
    if z_mkt.is_dir() and (z_mkt / "feige.py").is_file():
        ver = _read_feige_version(z_mkt)
        findings.append({
            "host": "ZCode 本地市场副本",
            "path": str(z_mkt),
            "version": ver,
            "stale": ver != cur_ver,
            "note": f"已对齐 v{cur_ver}" if ver == cur_ver else f"市场副本仍为 v{ver}（当前仓库 v{cur_ver}）",
        })

    # 3. kimi-code shim
    k_shim = home / ".kimi-code" / "hooks" / "feige_kimi_stop.py"
    if k_shim.is_file():
        k_txt = _read(k_shim).replace("\\", "/")
        ok_k = repo_norm.lower() in k_txt.lower()
        findings.append({
            "host": "kimi-code Hook Shim",
            "path": str(k_shim),
            "version": cur_ver if ok_k else "未知",
            "stale": not ok_k,
            "note": "直接透传至当前源码仓库 hooks/stop.py" if ok_k else "未指向当前仓库路径",
        })
    return findings


def _copy_repo_to_dir(dest: Path) -> None:
    """将当前仓库运行时核心文件同步到目标插件目录（不复制 .git/.venv/__pycache__）。"""
    dest.mkdir(parents=True, exist_ok=True)
    files = ("feige.py", "feige_interactive.py", "webui.py", "pyproject.toml", "README.md", "CHANGELOG.md")
    dirs = ("hooks", "adapters", "assets", "skills", ".claude-plugin", ".zcode-plugin")
    for fname in files:
        src = REPO / fname
        if src.is_file():
            shutil.copy2(src, dest / fname)
    for dname in dirs:
        src_d = REPO / dname
        if src_d.is_dir():
            dst_d = dest / dname
            shutil.copytree(src_d, dst_d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))


def sync_adapters_to_latest(home: Path | None = None) -> tuple[list[str], list[str]]:
    """将本机已安装的 feige 宿主钩子与插件缓存/市场副本同步对齐到当前仓库版本。"""
    home = home or Path.home()
    cur_ver = feige.__version__
    updated: list[str] = []
    errors: list[str] = []

    # 1. 更新 Claude Code ~/.claude/settings.json 的 Stop hook 指向当前仓库 hooks/stop.py
    c_set = home / ".claude" / "settings.json"
    if c_set.is_file():
        try:
            raw = c_set.read_text(encoding="utf-8")
            doc = json.loads(raw)
            repo_stop = str((REPO / "hooks" / "stop.py")).replace("\\", "/")
            changed = False
            for grp in (doc.get("hooks") or {}).get("Stop") or []:
                for hk in grp.get("hooks") or []:
                    cmd = str(hk.get("command") or "")
                    if "feige-fry-cards" in cmd and ("stop.py" in cmd or "stop_notify.py" in cmd):
                        new_cmd = f'python "{repo_stop}"'
                        if cmd != new_cmd:
                            hk["command"] = new_cmd
                            changed = True
            if changed:
                shutil.copy2(c_set, str(c_set) + ".bak-feige-sync")
                tmp = str(c_set) + ".tmp"
                Path(tmp).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, c_set)
                updated.append("Claude Code settings.json → 已切换至仓库 hooks/stop.py")
        except Exception as exc:
            errors.append(f"Claude Code settings.json 失败：{exc}")

    # 2. 同步各插件缓存与市场副本（同时更新已有旧版目录并生成当前版本目录）
    cache_roots = [
        ("Codex 插件缓存", home / ".codex" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards"),
        ("Mirasim Codex Relay 缓存", home / ".mirasim" / "agent-homes" / "codex-relay" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards"),
        ("ZCode 插件缓存", home / ".zcode" / "cli" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards"),
    ]
    for label, base in cache_roots:
        if not base.is_dir():
            continue
        try:
            targets = [c for c in base.iterdir() if c.is_dir()]
            ver_dir = base / cur_ver
            if ver_dir not in targets:
                targets.append(ver_dir)
            for t in targets:
                _copy_repo_to_dir(t)
            updated.append(f"{label} → 已同步 v{cur_ver}")
        except Exception as exc:
            errors.append(f"{label} 同步失败：{exc}")

    z_mkt = home / ".zcode" / "cli" / "plugins" / "marketplaces" / "feige-fry-cards"
    if z_mkt.is_dir():
        try:
            _copy_repo_to_dir(z_mkt)
            updated.append(f"ZCode 本地市场副本 → 已同步 v{cur_ver}")
        except Exception as exc:
            errors.append(f"ZCode 本地市场副本失败：{exc}")

    z_inst = home / ".zcode" / "cli" / "plugins" / "installed_plugins.json"
    if z_inst.is_file():
        try:
            zdoc = json.loads(z_inst.read_text(encoding="utf-8"))
            z_changed = False
            for ent in zdoc.get("plugins") or []:
                if isinstance(ent, dict) and ent.get("name") == "feige-fry-cards":
                    new_path = str(home / ".zcode" / "cli" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards" / cur_ver)
                    if ent.get("version") != cur_ver or ent.get("installPath") != new_path:
                        ent["version"] = cur_ver
                        ent["installPath"] = new_path
                        z_changed = True
            if z_changed:
                shutil.copy2(z_inst, str(z_inst) + ".bak-feige-sync")
                tmp = str(z_inst) + ".tmp"
                Path(tmp).write_text(json.dumps(zdoc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, z_inst)
                updated.append(f"ZCode installed_plugins.json → v{cur_ver}")
        except Exception as exc:
            errors.append(f"ZCode installed_plugins.json 失败：{exc}")

    return updated, errors


def page_adapters(home: Path | None = None, msg: str = "", err: str = "") -> bytes:
    """五家宿主接入与本机缓存版本排查，支持显式点击对齐至当前仓库版本。"""
    home = home or Path.home()
    rows = []
    plugin = "feige-fry-cards"

    z_ok = plugin in _read(home / ".zcode" / "cli" / "plugins" / "installed_plugins.json")
    rows.append(("ZCode", "已安装 feige-fry-cards 插件" if z_ok
                 else "未安装插件（ZCode 插件市场添加本仓库目录后安装）",
                 z_ok, "README「插件安装 → ZCode」"))

    c_plugin = (
        plugin in _read(home / ".claude" / "plugins" / "installed_plugins.json")
        or plugin in _read(home / ".claude.json")
        or (home / ".claude" / "plugins" / "data" / "feige-fry-cards-inline").exists()
    )
    settings = _read(home / ".claude" / "settings.json")
    c_manual = "stop_notify.py" in settings or "hooks/stop.py" in settings
    c_ok = c_plugin or c_manual
    c_note = ("已安装 feige-fry-cards 插件" if c_plugin
              else "已手动配置 Stop hook" if c_manual
              else "未接入（/plugin marketplace add 本仓库目录后安装）")
    rows.append(("Claude Code", c_note, c_ok, "README「插件安装 → Claude Code」"))

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
    k_norm = k_cfg.replace(chr(92), "/")
    k_ok = any(x in k_norm for x in ("stop_notify.py", "hooks/stop.py", "feige_kimi_stop.py", "feige"))
    rows.append(("kimi-code", "已手挂 Stop hook" if k_ok
                 else "未接入（~/.kimi-code/config.toml 手挂，见 README「插件安装 → kimi-code」）",
                 k_ok, "README「插件安装 → kimi-code」"))

    body = _banner(msg, err)
    body += "<div class='card'><h3>🔌 宿主接入概览</h3><table><tr><th>agent</th><th>状态</th><th>说明 / 指引</th></tr>"
    for name, note, ok, guide in rows:
        body += (f"<tr><td>{esc(name)}</td>"
                 f"<td><span class='badge {'ok' if ok else 'bad'}'>{'已接入' if ok else '未接入'}</span></td>"
                 f"<td>{note}<br><span class='small'>→ {esc(guide)}</span></td></tr>")
    body += ("</table><p class='small' style='margin:8px 0 0'>本卡仅做只读检测；"
             "hook 跑在 agent 启动时的环境快照里——改过环境变量或路径请新开宿主会话。</p></div>")

    findings = inspect_adapter_caches(home)
    if findings:
        stale_cnt = sum(1 for f in findings if f["stale"])
        cache_banner = (
            f"<div class='mode-banner'>⚠️ 检测到 <b>{stale_cnt}</b> 处旧缓存或过期路径，发卡可能仍在跑老代码！"
            f"点击下方按钮即可自动备份并对齐至当前仓库 <code>v{esc(VERSION)}</code>。</div>"
            if stale_cnt else
            f"<div class='mode-banner ok-mode'>🟢 全部 {len(findings)} 处插件路径与缓存均已同步至 <code>v{esc(VERSION)}</code>。</div>"
        )
        crows = "".join(
            f"<tr><td>{esc(f['host'])}</td>"
            f"<td><span class='badge {'warn' if f['stale'] else 'ok'}'>"
            f"{'待同步 (' + esc(f['version']) + ')' if f['stale'] else 'v' + esc(f['version'])}</span></td>"
            f"<td><code>{esc(f['path'])}</code><br><span class='small'>{esc(f['note'])}</span></td></tr>"
            for f in findings
        )
        body += (
            f"<div class='card'><h3>📦 本机宿主缓存与 Hook 路径排查</h3>{cache_banner}"
            f"<form method='post' action='/adapters/sync' style='margin:0 0 10px'>"
            f"<button class='primary' type='submit'>{ICONS['refresh']} 一键对齐所有宿主与缓存至 v{esc(VERSION)}</button>"
            f"</form>"
            f"<table><tr><th>目标位置</th><th>版本</th><th>路径与详情</th></tr>{crows}</table></div>"
        )
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
    server_version = f"feige-webui/{VERSION}"

    def log_message(self, fmt, *args):  # 静默访问日志（避免路径里的业务参数刷屏）
        pass

    def _send(self, data: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self) -> bytes:
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query or "")
        if self.command == "GET":
            if u.path.startswith("/assets/vendor-logos/"):
                fname = u.path.rsplit("/", 1)[-1]
                if (fname.endswith(".png") and "/" not in fname and "\\" not in fname
                        and ".." not in fname):
                    asset_path = (feige._ASSETS_VENDOR_DIR / fname).resolve()
                    if asset_path.parent == feige._ASSETS_VENDOR_DIR.resolve() and asset_path.is_file():
                        self._send(asset_path.read_bytes(), content_type="image/png")
                        return b""
                self._send(b"not found", status=404, content_type="text/plain; charset=utf-8")
                return b""
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
        if u.path == "/api/llm/models":
            try:
                data = json.loads(body.decode("utf-8", "replace")) if body else {}
            except Exception:
                data = {}
            base_url = str(data.get("base_url") or "").strip()
            api_key = str(data.get("api_key") or "").strip()
            try:
                models = feige.fetch_models(base_url=base_url, api_key=api_key)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                resp_bytes = json.dumps({"ok": True, "models": models}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)
                return b""
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                err_bytes = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)
                return b""
        if u.path == "/api/preview":
            try:
                data = json.loads(body.decode("utf-8", "replace")) if body else {}
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                self._send(json.dumps({"ok": False, "error": "非法 JSON"}, ensure_ascii=False).encode("utf-8"),
                           status=400, content_type="application/json; charset=utf-8")
                return b""
            res = build_preview_payload(data)
            self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"),
                       status=200 if res["ok"] else 400,
                       content_type="application/json; charset=utf-8")
            return b""
        if u.path == "/routes":
            return handle_routes_post(body)
        if u.path == "/test":
            return handle_test_post(body)
        if u.path == "/ws/sync":
            st = feige_interactive.get_ws_manager().sync_with_config()
            note = f"长连接状态已同步：{st['state']}" + (f"（{st['error']}）" if st.get("error") else "")
            return page_status(msg=note)
        if u.path == "/adapters/sync":
            updated, errors = sync_adapters_to_latest()
            msg_txt = "；".join(updated) if updated else "所有检测到的宿主与缓存已是最新版本"
            err_txt = "；".join(errors) if errors else ""
            return page_adapters(msg=msg_txt, err=err_txt)
        if u.path == "/settings":
            resp = handle_settings_post(body)
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            card_name = (form.get("form") or [""])[0]
            if card_name in ("feishu", "interactive") and b'<div class="err">' not in resp:
                try:
                    st = feige_interactive.get_ws_manager().sync_with_config()
                    if card_name == "interactive" and (form.get("_next") or [""])[0] != "preview":
                        ws_note = f" · 长连接：{st['state']}" + (f"（{st['error']}）" if st.get("error") else "")
                        resp = page_studio(msg=f"{_CARD_TITLES[card_name]}已保存{ws_note}")
                except Exception:
                    pass
            if (form.get("_next") or [""])[0] == "preview" and b'<div class="err">' not in resp:
                self.send_response(303)
                self.send_header("Location", "/preview?saved=1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return b""
            return resp
        return page("404", "<div class='err'>页面不存在</div>")

    def _handle(self) -> None:
        reason = request_allowed(self.command, self.headers.get("Host", ""),
                                 self.headers.get("Origin", ""),
                                 self.headers.get("Referer", ""),
                                 self.server.server_address[1])
        if reason:
            try:
                cl = int(self.headers.get("Content-Length") or 0)
                if 0 < cl <= 65536:
                    self.rfile.read(cl)
            except (ValueError, OSError):
                pass
            self._send(page("拒绝", f"<div class='err'>{esc(reason)}</div>"), status=403)
            return
        try:
            res = self._dispatch()
            if res:
                self._send(res)
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


# ── 后台保活：start 拉起脱离终端的守护进程，worker 崩了自动重启 ────────────────
#
# 进程树：start（前台，拉起后即退）→ supervise（守护，写 pidfile）→ worker（真服务）。
# 终端关闭杀不掉守护（Windows DETACHED_PROCESS / POSIX 新会话）；worker 活不长
# 就退避 5s 再拉，防止崩溃循环打爆日志；stop 按 pidfile 整树杀。
# pidfile/log 默认放 ~/.feige-studio.json / ~/.feige-studio.log，
# FEIGE_STUDIO_PID_FILE / FEIGE_STUDIO_LOG_FILE 可覆盖（离线测试用）。

# 守护进程树全程不落可见控制台窗口：没控制台的进程生出控制台程序时，Windows 会
# 给它新开一个可见空窗口——每一层 spawn 都必须带脱离标志
if os.name == "nt":
    _DETACHED_FLAGS = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
else:  # POSIX：脱离终端靠 start_new_session
    _DETACHED_FLAGS = 0


def studio_pid_file() -> Path:
    return Path(os.environ.get("FEIGE_STUDIO_PID_FILE")
                or Path.home() / ".feige-studio.json")


def studio_log_file() -> Path:
    return Path(os.environ.get("FEIGE_STUDIO_LOG_FILE")
                or Path.home() / ".feige-studio.log")


def studio_log(msg: str) -> None:
    """守护进程的诊断日志；超 1MB 轮转 .1（口径同 feige.open_log_file）。"""
    path = studio_log_file()
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            path.rename(Path(str(path) + ".1"))
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def port_alive(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) 在 Windows 是无条件 TerminateProcess，绝不能用——查句柄
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程在，只是没权限发信号


def read_studio_pid() -> dict | None:
    try:
        data = json.loads(studio_pid_file().read_text(encoding="utf-8"))
        pid, port = int(data.get("pid") or 0), int(data.get("port") or 0)
        return {"pid": pid, "port": port} if pid > 0 and port > 0 else None
    except Exception:
        return None


def write_studio_pid(pid: int, port: int) -> None:
    studio_pid_file().write_text(
        json.dumps({"pid": pid, "port": port,
                    "started": time.strftime("%Y-%m-%d %H:%M:%S")}),
        encoding="utf-8")


def clear_studio_pid() -> None:
    try:
        studio_pid_file().unlink(missing_ok=True)
    except OSError:
        pass


def kill_tree(pid: int) -> None:
    """整树杀守护（Windows taskkill /T 连带 worker；POSIX 杀整个新会话进程组）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def _supervise_decision(rc: int, alive_for: float, port_ok: bool) -> tuple[bool, float]:
    """worker 退出后的裁决：(是否继续保活, 重启前等待秒数)。

    worker 绑不上端口（rc=3）且端口已有人响应 = 另一实例接管了，功成身退；
    其余一律重启——活得太短（<3s，崩溃循环）退避 5s，正常退出立即拉起。
    """
    if rc == 3 and port_ok:
        return False, 0.0
    return True, 5.0 if alive_for < 3 else 1.0


def cmd_start(port: int) -> int:
    if port_alive(port):
        info = read_studio_pid()
        hint = f"（守护 pid {info['pid']}）" if info and info["port"] == port else ""
        print(f"🕊️ feige Studio 已在运行{hint}: http://127.0.0.1:{port}")
        return 0
    stale = read_studio_pid()
    if stale and pid_alive(stale["pid"]):
        # 守护还活着但端口没响应：worker 在崩溃循环里，整树杀掉重新拉
        print(f"🕊️ 守护进程 pid {stale['pid']} 存活但端口 {port} 无响应，重启守护…")
        kill_tree(stale["pid"])
        time.sleep(1.0)
    clear_studio_pid()
    script = str(Path(__file__).resolve())
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_FLAGS
    else:
        kwargs["start_new_session"] = True  # 脱离终端：控制台关了守护照样活
    with studio_log_file().open("a", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(
            [sys.executable, script, "supervise", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, **kwargs)
    for _ in range(40):  # 最多等 10s
        if port_alive(port):
            print(f"🕊️ feige Studio 已后台拉起: http://127.0.0.1:{port}"
                  f"（守护 pid {proc.pid}，保活日志 {studio_log_file()}）")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    print(f"🕊️ 守护进程已启动但端口 {port} 尚未就绪"
          + (f"（进程已退出 rc={proc.returncode}）" if proc.poll() is not None else "")
          + f"，详情看日志：{studio_log_file()}")
    return 1


def cmd_stop() -> int:
    info = read_studio_pid()
    if info and pid_alive(info["pid"]):
        kill_tree(info["pid"])
        for _ in range(20):  # 最多等 5s 端口释放
            if not port_alive(info["port"]):
                break
            time.sleep(0.25)
        clear_studio_pid()
        print(f"🕊️ feige Studio 已停止（pid {info['pid']}）")
        return 0
    clear_studio_pid()
    if info and port_alive(info["port"]):
        print(f"🕊️ 端口 {info['port']} 仍有服务响应，但守护 pid {info['pid']} 已不在"
              "——多半是前台进程，请自行 Ctrl+C。")
        return 1
    print("🕊️ feige Studio 未在运行")
    return 0


def cmd_status(cli_port: int | None = None) -> int:
    info = read_studio_pid()
    port = cli_port or (info or {}).get("port") or 8787
    url = f"http://127.0.0.1:{port}"
    if port_alive(port):
        if info and pid_alive(info["pid"]):
            print(f"🕊️ feige Studio 运行中（守护 pid {info['pid']}）: {url}")
        else:
            print(f"🕊️ feige Studio 端口在响应（无守护，疑为前台进程）: {url}")
        return 0
    if info and pid_alive(info["pid"]):
        print(f"🕊️ 守护进程存活（pid {info['pid']}）但端口 {port} 无响应——worker "
              f"可能在崩溃循环，日志：{studio_log_file()}")
        return 1
    print("🕊️ feige Studio 未在运行")
    return 1


def cmd_supervise(port: int) -> int:
    """保活主循环：写 pidfile，worker 死了按退避策略拉起，永不主动退出。"""
    write_studio_pid(os.getpid(), port)
    studio_log(f"supervise start (pid {os.getpid()}, port {port})")
    script = str(Path(__file__).resolve())
    while True:
        with studio_log_file().open("a", encoding="utf-8", errors="replace") as logf:
            child = subprocess.Popen(
                [sys.executable, script, "worker", "--port", str(port)],
                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                creationflags=_DETACHED_FLAGS)
        t0 = time.monotonic()
        rc = child.wait()
        go_on, delay = _supervise_decision(rc, time.monotonic() - t0, port_alive(port))
        studio_log(f"worker exited rc={rc} after {time.monotonic() - t0:.1f}s"
                   + ("" if go_on else "; port owned by another instance, exiting"))
        if not go_on:
            clear_studio_pid()
            return 0
        time.sleep(delay)


def cmd_worker(port: int) -> int:
    """真服务进程：绑不上端口先重试 10s（stop/start 竞态、TIME_WAIT 都让它过去）。"""
    deadline = time.monotonic() + 10
    srv = None
    while srv is None:
        try:
            srv = Server(("127.0.0.1", port), Handler)
        except OSError:
            if time.monotonic() >= deadline:
                studio_log(f"port {port} busy for 10s, worker giving up (rc 3)")
                return 3
            time.sleep(0.5)
    try:
        ws_st = feige_interactive.get_ws_manager().sync_with_config()
        if ws_st.get("enabled"):
            studio_log(f"ws listener state={ws_st.get('state')} err={ws_st.get('error') or '-'}")
    except Exception as exc:
        studio_log(f"ws listener init skipped: {feige.redact(exc)}")
    studio_log(f"worker serving http://127.0.0.1:{port} (pid {os.getpid()})")
    print(f"🕊️ feige Studio worker: http://127.0.0.1:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            feige_interactive.get_ws_manager().stop()
        except Exception:
            pass
        srv.server_close()
    return 0


def cmd_run(port: int) -> int:
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        sys.exit(f"🕊️ 端口 {port} 不可用（{exc}），换一个：python webui.py --port <N>")
    try:
        feige_interactive.get_ws_manager().sync_with_config()
    except Exception:
        pass
    print(f"🕊️ feige Studio: http://127.0.0.1:{port} "
          "（Ctrl+C 退出；关终端也活着：python webui.py start）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            feige_interactive.get_ws_manager().stop()
        except Exception:
            pass
        srv.server_close()
        print("bye")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="🕊️ feige Studio（仅 127.0.0.1，勿暴露端口）")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("action", nargs="?", default="run",
                    choices=["run", "start", "stop", "restart", "status",
                             "supervise", "worker"],
                    help="run=前台（默认）· start/stop/restart/status=后台保活 · "
                         "supervise/worker=保活内部进程，勿手动调")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.action == "run":
        return cmd_run(args.port)
    if args.action == "start":
        return cmd_start(args.port)
    if args.action == "stop":
        return cmd_stop()
    if args.action == "restart":
        cmd_stop()
        return cmd_start(args.port)
    if args.action == "status":
        return cmd_status(args.port)
    if args.action == "supervise":
        return cmd_supervise(args.port)
    return cmd_worker(args.port)


if __name__ == "__main__":
    sys.exit(main())
