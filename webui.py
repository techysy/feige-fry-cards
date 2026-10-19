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
VERSION = "1.0.0"

# 视觉语言对齐 CreditDaddy / ImgMark（两者同一套：CSS 变量双主题
# html[data-theme="dark"]、980px 内容列、胶囊导航、14px 圆角卡片、深色主按钮）。
_CSS = """
:root{
  --bg:#f5f6f8; --card:#ffffff; --surface-2:#fafafa; --tertiary:#f0f0f0;
  --hover:#f5f6f8; --active:#eef0f3;
  --line:#eeeeee; --border-subtle:#eeeeee; --border:#e5e5e5; --border-strong:#cccccc;
  --txt:#1f1f1f; --muted:#5f6368; --text-3:#999999;
  --btn-bg:#1f1f1f; --btn-fg:#ffffff;
  --pri:#22c55e; --pri-dk:#16a34a; --ok:#16a34a; --bad:#dc2626; --warn-c:#d97706;
  --radius:14px;
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

/* header：品牌左、工具右，与导航/内容同收 980px 居中列 */
header{display:flex;align-items:center;justify-content:space-between;gap:12px;
  max-width:980px;margin:0 auto;padding:16px 22px 4px}
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
   左缘对齐居中的 980px 内容列，窄屏退到 22px */
.nav{display:flex;gap:4px;padding:4px;width:fit-content;max-width:calc(100% - 44px);
  margin:10px auto 0;background:var(--card);border:1px solid var(--border-subtle);
  border-radius:12px;overflow-x:auto}
@supports (margin:10px 0 0 max(22px,1px)){
  .nav{margin:10px 0 0 max(22px,calc((100% - 980px)/2))}}
.nav a{display:inline-flex;align-items:center;gap:6px;border:none;background:transparent;
  color:var(--muted);font-size:13px;font-weight:600;padding:7px 14px;border-radius:9px;
  cursor:pointer;white-space:nowrap;text-decoration:none;transition:background .15s,color .15s}
.nav a:hover{color:var(--txt);background:var(--hover)}
.nav a.on{background:var(--btn-bg);color:var(--btn-fg)}

main{max-width:980px;margin:0 auto;padding:12px 22px 30px;display:flex;
  flex-direction:column;gap:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;
  align-items:start}
@media (max-width:520px){.grid{grid-template-columns:1fr}}

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

pre{background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:10px;
  padding:10px;overflow:auto;font:12px/1.6 Consolas,"Cascadia Mono",monospace}
code{font:12px/1.4 Consolas,"Cascadia Mono",monospace;background:var(--tertiary);
  padding:1px 5px;border-radius:5px}
/* 预览页卡片外观示意的状态条（背景色由内联 style 给） */
.bar{height:6px;border-radius:12px 12px 0 0}

/* 表单行：12px 灰标签 + 徽章 + 全宽控件 */
.frow{margin:0 0 12px}
.frow .lab{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
.frow .lab .sub{color:var(--text-3)}
.frow input[type=text],.frow input[type=password]{margin-top:0}
.clearline{font-size:11px;color:var(--text-3);margin-top:3px}
.clearline label,.checkrow label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.checkrow{font-size:13px;margin:0 0 12px}
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
(function(){
  var box = document.getElementById('footer-chips-box');
  if(!box) return;
  var hiddenInput = document.getElementById('footer_fields_val');
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

  function dropRefChip(b, x, y) {
    var chips = Array.prototype.slice.call(b.querySelectorAll('.field-chip')).filter(function(c){ return c !== dragChip; });
    for(var i=0; i<chips.length; i++){
      var r = chips[i].getBoundingClientRect();
      if(y > r.bottom) continue;
      if(y >= r.top && x >= r.left + r.width / 2) continue;
      return chips[i];
    }
    return null;
  }

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
    var ref = dropRefChip(box, e.clientX, e.clientY);
    if(ref) ref.classList.add('drop-mark');
  });
  box.addEventListener('drop', function(e){
    if(!dragChip) return;
    e.preventDefault();
    var ref = dropRefChip(box, e.clientX, e.clientY);
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
            f'<body><header><div class="brand"><span class="logo-emoji">{ICONS["dove"]}</span>'
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
    badge = (f"<span class='tag on'>已配置 · {esc(src)}</span>" if src
             else "<span class='tag'>未配置</span>")
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



FOOTER_FIELD_NAMES = {
    "project": "项目",
    "model": "模型",
    "thinking": "思考数",
    "tools": "工具数",
    "context": "上下文",
    "tokens": "Tokens",
    "elapsed": "耗时",
}

def _footer_chips_html() -> str:
    saved = feige.load_settings().get("footer_fields")
    if saved is None:
        saved_list = list(feige.DEFAULT_FOOTER_FIELDS)
    else:
        saved_list = [str(x).strip() for x in saved if str(x).strip()]

    chips = []
    active_keys = set()
    for k in saved_list:
        if k in FOOTER_FIELD_NAMES:
            chips.append((k, FOOTER_FIELD_NAMES[k], True))
            active_keys.add(k)
    for k in feige.DEFAULT_FOOTER_FIELDS:
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
    return (
        "<div class='frow'>"
        f"<div class='lab'>脚注字段（footer） <span class='tag on'>可拖动排序</span></div>"
        "<span class='field-tip'>点按切换显隐（置灰 = 隐藏）；按住拖动调整展示顺序。</span>"
        f"<div class='field-chips' id='footer-chips-box'>{''.join(chips_html)}</div>"
        f"<input type='hidden' name='footer_fields' id='footer_fields_val' value='{esc(active_val)}'>"
        "</div>"
    )

def _save_btn(card_id: str) -> str:
    return (f"<p><input type='hidden' name='form' value='{card_id}'>"
            f"<button class='primary' type='submit'>{ICONS['save']} 保存本卡片</button></p>")


def _banner(msg: str, err: str) -> str:
    out = ""
    if err:
        out += f'<div class="err">{ICONS["alert"]} {esc(err)}</div>'
    if msg:
        out += f'<div class="msg">{ICONS["check"]} {esc(msg)}</div>'
    return out


# ── 页面：配置（Studio 主页）──────────────────────────────────────────────────

def _warn_html() -> str:
    return (f'<div class="warn">{ICONS["save"]} 保存只写本页白名单键进 <code>~/.feige-settings.json</code>'
            '（写前自动备份 <code>.bak</code>），其余内容原样保留；保存即生效——后台发送'
            f'进程每次发卡重新读取，无需重启宿主。<br>{ICONS["lock"]} 密钥与 webhook 只显「已配置」徽章，'
            '输入框永不回显：留空 = 不修改，勾选「清除」才删除。已配环境变量时环境变量优先'
            '（徽章标注来源），此处改动对被遮蔽的键不生效。</div>')


def page_studio(msg: str = "", err: str = "") -> bytes:
    llm = feige.llm_config()
    llm_state = ("<span class='tag on'>已启用</span>" if llm["enabled"]
                 else "<span class='tag'>未启用（正文超长直接截断）</span>")
    cur_model = feige._env(_ENV_FOR_KEY.get("llm_model", "")) or ""
    cards = {
        "feishu": (
            f"<div class='card'><h3>{ICONS['feishu']} 飞书渠道</h3><form method='post' action='/settings'>"
            + _field("feishu_webhook", "自定义机器人 webhook",
                     "https://open.feishu.cn/open-apis/bot/v2/hook/…", "填了就走 webhook 通道")
            + _field("feishu_webhook_secret", "webhook 签名密钥", hint="机器人开「签名校验」才需要")
            + _field("feishu_app_id", "App ID（CardKit）", "cli_…", "不用 webhook 时填")
            + _field("feishu_app_secret", "App Secret（CardKit）")
            + _field("feishu_base_url", "飞书域名", feige.DEFAULT_BASE_URL, "Lark 国际版填 open.larksuite.com")
            + _field("feishu_chat_id", "接收群 chat_id", "oc_…", "CardKit 通道必配（全项目默认群）")
            # 每群一个接收群：键名 feishu_chat_id__<项目名>，项目名必须与路由标签逐字一致
            + _field("feishu_chat_id__10router", "10Router 群 chat_id（按项目覆盖）", "oc_…",
                     "键名 feishu_chat_id__<项目名>：留空则该项目沿用上面的默认群")
            + _save_btn("feishu") + "</form></div>"),
        "dingtalk": (
            f"<div class='card'><h3>{ICONS['dingtalk']} 钉钉渠道</h3><form method='post' action='/settings'>"
            + _field("dingtalk_webhook", "机器人 webhook")
            + _field("dingtalk_secret", "加签密钥", hint="SEC 开头，安全设置选「加签」才需要")
            + "<p class='small'>钉钉无交互卡，发 markdown 消息；需显式 <code>--channel</code> "
              "或群路由指定。</p>"
            + _save_btn("dingtalk") + "</form></div>"),
        "telegram": (
            f"<div class='card'><h3>{ICONS['telegram']} Telegram 渠道</h3><form method='post' action='/settings'>"
            + _field("telegram_bot_token", "Bot token", hint="BotFather 发放，形如 123456:ABC-…")
            + _field("telegram_chat_id", "目标 chat", "-100…（群）或个人 id")
            + "<p class='small'>纯文本版式（≤4096 UTF-16 单元）；需显式渠道或路由指定。</p>"
            + _save_btn("telegram") + "</form></div>"),
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
            + "<div class='frow'><label><input type='checkbox' name='llm_off' value='1' "
              + ("checked" if feige.load_settings().get("summarize") is False else "")
              + "> 显式关闭总结（即使配了 key 也只截断）</label></div>"
            + "<p class='small'>正文超长时先由后台发送进程调 LLM 浓缩成战报摘要，"
              "未配置/失败退化为硬截断；宿主 hook 前台零网络阻塞。</p>"
            + _save_btn("llm") + "</form></div>"),
        "behavior": (
            f"<div class='card'><h3>{ICONS['behavior']} 行为开关</h3><form method='post' action='/settings'>"
            + _select_field("stats_scope", "统计口径",
                            {"turn": "单轮（只算最后一轮）",
                             "session": "全程（整个会话累计）"},
                            feige.stats_scope())
            + _footer_chips_html()
            + _field("debounce_seconds", "收尾去抖秒数", "0", "同会话 N 秒内又收尾只发最后一张；0=每轮都发")
            + _field("routes_file", "群路由文件路径", "~/.feige-routes.json", "留空用默认")
            + "<p class='small'>宿主 hook 的启用开关（hook_notify）与凭据在各自插件设置里"
              "也有入口——插件设置注入为环境变量，优先于本页。</p>"
            + _save_btn("behavior") + "</form></div>"),
    }
    body = (_banner(msg, err) + _warn_html()
            + "<div class='grid'>" + cards["feishu"] + cards["llm"]
            + cards["dingtalk"] + cards["telegram"] + cards["behavior"] + "</div>"
            + f"<div class='card'><h3>{ICONS['routes']} 群路由</h3><p class='small'>项目 → 多群 fanout "
              "（钉钉 / Telegram 的凭据配在这里）。路由文件里的密钥建议写 <code>$ENV</code> 引用。</p>"
              "<p><a href='/routes'>编辑群路由 →</a></p></div>")
    return page("配置", body, tab="config")


def _select_field(skey: str, label: str, options: dict, current: str) -> str:
    src = key_source(skey)
    badge = (f"<span class='tag on'>已配置 · {esc(src)}</span>" if src
             else "<span class='tag'>默认</span>")
    opts = "".join(f"<option value='{esc(k)}' {'selected' if current == k else ''}>{esc(v)}</option>"
                   for k, v in options.items())
    return (f"<div class='frow'><div class='lab'>{esc(label)} {badge}</div>"
            f"<select name='{skey}'>{opts}</select></div>")


# ── POST /settings：按卡片白名单保存 ─────────────────────────────────────────

_CARD_KEYS = {
    "feishu": ["feishu_webhook", "feishu_webhook_secret", "feishu_app_id",
               "feishu_app_secret", "feishu_base_url", "feishu_chat_id",
               # 按项目覆盖接收群：键名 feishu_chat_id__<项目名>（与路由标签逐字一致）
               "feishu_chat_id__10router"],
    "dingtalk": ["dingtalk_webhook", "dingtalk_secret"],
    "telegram": ["telegram_bot_token", "telegram_chat_id"],
    "llm": ["llm_api_key", "llm_base_url", "llm_model", "llm_max_chars"],
    "behavior": ["stats_scope", "debounce_seconds", "routes_file", "footer_fields"],
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
        if key == "footer_fields":
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


# ── 页面：卡片预览 ────────────────────────────────────────────────────────────

# 外观示意的状态条配色：ok/error 跟随主题 token，running 用家族蓝（飞书卡蓝头）
_STATUS_COLOR = {"running": "#3b82f6", "ok": "var(--ok)", "error": "var(--bad)"}


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
    server_version = f"feige-webui/{VERSION}"

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
