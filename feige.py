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
        [--tokens N] \
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
    FEIGE_LLM_API_KEY                           正文总结 LLM 的 API key（OpenAI 兼容，可选）
    FEIGE_LLM_BASE_URL                          总结 API base，默认 https://api.openai.com/v1
    FEIGE_LLM_MODEL                             总结模型，默认 gpt-4o-mini
    FEIGE_LLM_MAX_CHARS                         总结目标字数，默认 400
    FEIGE_LLM_MIN_CHARS                         总结触发字数阈值，默认 300（0=只在超渠道上限才总结）
    FEIGE_SUMMARIZE                             0/false 显式关闭总结（即使已配 key）

配置来源（优先级从高到低）：真实环境变量 > 插件 userConfig（落到环境变量）>
设置文件 ~/.feige-settings.json（Studio 配置页写入，键名见 _SETTINGS_ENV）。
渠道凭据、总结 LLM、行为开关都能只配在设置文件里，不必再碰环境变量。

设计不变量（继承自家族踩坑）：摘要是战报不是镜像（正文超阈值/超长先试 LLM 总结，未配置/
失败退化为硬截断）；统计面板只在完成态（ok/error）做单次脚注，running 态不堆指标；
fail-open——发卡失败只落日志，绝不抛炸调用方；密钥只走环境变量或本地设置文件
（绝不入库），错误信息与页面回显里 token/secret/webhook/api key 一律打码。总结只在
后台发送进程里跑（dry-run/预览在 hook 前台同步执行，绝不发起 LLM 网络调用，不阻塞宿主）。
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

# 主插件版本号单一来源：面板（webui.VERSION）从这里读，两份插件清单
# （.claude-plugin / .zcode-plugin 的 plugin.json + marketplace.json）同步一致，
# 由测试锁住防漂移。
__version__ = "2.4.0"

DEFAULT_BASE_URL = "https://open.feishu.cn"
STREAMING_ELEMENT_ID = "streaming_content"  # 流式正文元素 id（家族固定口径）
UNIFIED_PANEL_ELEMENT_ID = "unified_panel"  # 底部统一折叠面板元素 id（家族固定口径）
# 渠道体量上限（官方口径 + 余量）。按字符截断不够：中文 UTF-8 占 3 字节，24k 字就是 72KB
BODY_MAX_BYTES = 15_000    # 飞书 webhook 请求体 ≤20KB、钉钉消息 ≤20000 字节，给卡片骨架留余量
DETAIL_PANEL_MAX_BYTES = 3_500  # 折叠面板详细正文预算，防撑爆 20KB webhook / 30KB CardKit 上限
CARD_WEBHOOK_SAFE_BYTES = 19_000
CARD_CARDKIT_SAFE_BYTES = 28_000
TELEGRAM_MAX_UNITS = 4096  # Telegram sendMessage text ≤4096，按 UTF-16 单元计（emoji 占 2）
TRUNC_NOTE = "\n\n…（内容过长已截断）"

# 状态 → header 着色（与 hermes-fry-cards 家族一致：流式中蓝、完成绿、中断/错误红）
STATUS_TEMPLATE = {"running": "blue", "ok": "green", "error": "red"}
STATUS_PREFIX = {"running": "🛠️", "ok": "✅", "error": "❌"}
PANEL_BORDER_COLOR = {"running": "blue", "ok": "green", "error": "red", "aborted": "yellow"}


# ── 通用小件 ─────────────────────────────────────────────────────────────────

# 环境变量名 → ~/.feige-settings.json 键（Studio 配置页写设置文件；环境变量优先）。
_SETTINGS_ENV = {
    "FEISHU_CARD_WEBHOOK": "feishu_webhook",
    "FEISHU_WEBHOOK_SECRET": "feishu_webhook_secret",
    "FEISHU_APP_ID": "feishu_app_id",
    "FEISHU_APP_SECRET": "feishu_app_secret",
    "FEISHU_BASE_URL": "feishu_base_url",
    "FEISHU_NOTIFY_CHAT_ID": "feishu_chat_id",
    "DINGTALK_WEBHOOK": "dingtalk_webhook",
    "DINGTALK_SECRET": "dingtalk_secret",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    "FEIGE_LLM_API_KEY": "llm_api_key",
    "FEIGE_LLM_BASE_URL": "llm_base_url",
    "FEIGE_LLM_MODEL": "llm_model",
    "FEIGE_LLM_MAX_CHARS": "llm_max_chars",
    "FEIGE_LLM_MIN_CHARS": "llm_min_chars",
    "FEIGE_STATS_SCOPE": "stats_scope",
    "FEIGE_DEBOUNCE_SECONDS": "debounce_seconds",
    "FEIGE_ROUTES_FILE": "routes_file",
    "FEIGE_FOOTER_FIELDS": "footer_fields",
    "FEIGE_PANEL_FIELDS": "panel_fields",
    "FEIGE_FOOTER_ENABLED": "footer_enabled",
    "FEIGE_CONTEXT_STYLE": "context_style",
    "FEIGE_PANEL_HOST_PREFIX": "panel_host_prefix",
    "FEIGE_INTERACTIVE": "feishu_interactive",
    "FEIGE_WS_ENABLED": "feishu_ws_enabled",
    "FEIGE_ALLOW_USERS": "interactive_allow_users",
    "FEIGE_VENDOR_LOGO": "vendor_logo_enabled",
    "FEIGE_PANEL_EXPANDED": "panel_expanded",
    "FEIGE_RESUME_TIMEOUT": "resume_timeout_seconds",
    "FEIGE_SESSIONS_FILE": "sessions_file",
    "FEIGE_CLAUDE_PERMISSION_MODE": "claude_permission_mode",
}


def _env(*names: str) -> str:
    """取第一个非空值：环境变量（含插件 userConfig 落进来的）> 设置文件。

    设置文件里的密钥同样参与 redact 打码（redact 就是逐个 _env 取值替换的）。
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    settings = None
    for name in names:
        key = _SETTINGS_ENV.get(name)
        if not key:
            continue
        if settings is None:
            settings = load_settings()
        value = str(settings.get(key) or "").strip()
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
        _env("FEIGE_LLM_API_KEY"),
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


# ── 正文超长 → LLM 总结（可配置，失败退化硬截断）───────────────────────────────

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
LLM_MAX_CHARS = 400        # 总结目标字数（近似值，最终仍按渠道字节上限兜底）
LLM_MIN_CHARS = 300        # 总结触发阈值：启用后正文超过这么多字就先浓缩成摘要，
                           # 而不是原样镜像（0 = 只在超渠道上限时才总结的旧口径）
LLM_INPUT_MAX_CHARS = 24_000  # 送进总结请求的原文上限，防超长上下文把请求撑炸
LLM_TIMEOUT = 45           # 秒；只在后台发送进程里等，不碰 hook 前台。免费/慢速池
                           # 20s 经常不够（实测超时回落原文），45s 换摘要稳定出卡


def llm_config() -> dict:
    """总结 LLM 配置：{"enabled", "api_key", "base_url", "model", "max_chars", "min_chars"}。

    取值口径统一走 _env（环境变量 > 设置文件）。没配 api_key 即未启用
    （enabled=False）——未配置是常态，不叫错误。
    """
    settings = load_settings()
    max_chars_raw = _env("FEIGE_LLM_MAX_CHARS")
    try:
        max_chars = max(int(max_chars_raw), 50) if max_chars_raw else LLM_MAX_CHARS
    except ValueError:
        max_chars = LLM_MAX_CHARS
    min_chars_raw = _env("FEIGE_LLM_MIN_CHARS")
    try:
        min_chars = max(int(min_chars_raw), 0) if min_chars_raw else LLM_MIN_CHARS
    except ValueError:
        min_chars = LLM_MIN_CHARS
    api_key = _env("FEIGE_LLM_API_KEY")
    raw_off = settings.get("summarize")  # 注意 False 是显式关闭，不能当「未配置」
    off = (_env("FEIGE_SUMMARIZE")
           or ("" if raw_off is None else str(raw_off))).strip().lower()
    return {
        "enabled": bool(api_key) and off not in ("0", "false", "no", "off"),
        "api_key": api_key,
        "base_url": (_env("FEIGE_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL).rstrip("/"),
        "model": _env("FEIGE_LLM_MODEL") or DEFAULT_LLM_MODEL,
        "max_chars": max_chars,
        "min_chars": min_chars,
    }


_SUMMARY_TAG_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
# 复述系统要求或思考约束的行指纹词
_ECHO_MARKERS = (
    "用户要求", "压缩成", "只输出", "摘要本身", "寒暄", "过程性叙述",
    "标签之外", "不得输出", "分析过程", "字数检查", "放进 <summary>",
    "标签之间", "战报摘要器", "不复述", "不写分析", "字数估算", "需保留",
)
# 过程独白行：「我需要把上面的汇报精简」「让我组织一下」「检查字数，应该在400字以内」「标签内。考虑是否...」「摘要草稿：」
_META_LINE_RE = re.compile(
    r"^(?:"
    r"让我们?|我们|"
    r"我(?:需要|来|会|将|先|准备|数一下|也?要|应该|想|得|看|整理|提炼|总结|保留|把|认为|估计)|"
    r"好的[，,！!。.\s]|明白了[，,！!]|收到[，,！!]|当然[，,！!]|好滴[，,！!]|OK[，,！!:\s]|Ok[，,！!:\s]|"
    r"标签内[。：:.\s]?|在标签内|标签中|标签之外|"
    r"注意(?:到|事项)?|需要注意|需注意|也要注意|"
    r"考虑(?:到|是否)?|"
    r"不要(?:写|输出|包含|有)|不能(?:写|输出|包含|有)|严禁|"
    r"要保证|确保|尽量|需保留|需要保留|"
    r"字数.{0,6}(?:估算|预估|统计|要求|控制|约|大约|以内|左右|符合)|"
    r"(?:检查|数一下|核对).{0,2}字数|"
    r"这个内容|这篇|本篇|助手汇报|收尾汇报|战报摘要|战报内容|"
    r"根据(?:要求|提示|上下文|汇报)|按照(?:要求|指示|规范)|"
    r"以下是|下面是|输出如下|结果如下|整理如下|总结如下|摘要如下|"
    r"(?:摘要|战报|内容)?(?:草稿|初稿|底稿|正文|总结)[：:]?$|"
    r"(?:思考过程|思考|分析过程|分析|推理过程|思路)[：:]?$"
    r")"
)


def _strip_monologue(text: str) -> str:
    """逐行剔掉复述要求的行与独白行。

    剔完为空返回空串——调用方 fit_body 会退化到硬截断原文，好过发一卡片独白。
    """
    kept = []
    for line in text.splitlines():
        s = line.replace("</summary>", "").replace("<summary>", "").strip()
        if not s:
            continue
        if sum(m in s for m in _ECHO_MARKERS) >= 2:
            continue  # 复述要求
        if _META_LINE_RE.match(s):
            continue  # 过程独白
        kept.append(s)
    return "\n".join(kept).strip()


def _extract_summary(text: str) -> str:
    """从总结模型的返回里剥出摘要本体。

    推理模型（hy4/GLM 等）会输出 <think> 思考块、碎碎念独白、草稿、复述系统要求、
    在摘要前后写分析过程和字数检查，直接镜像进卡片就是灾难现场（流口水）。
    处理机制：
    1. 彻底清除推理模型的显式思考标签（<think>...</think>, <thought>...</thought> 等）；
    2. 定位摘要标签：若存在 </summary>，取闭标签前最后一个开标签的内容（封堵多重 <summary> 嵌套泄露草稿）；
       若截断未闭合，取最后一个开标签之后的内容；
    3. 剥除代码块包裹（```...```）；
    4. 对提取后的摘要内容做二次独白与草稿前言清洗；
    5. 标签未配合或清洗后为空时兜底逐行去独白，依然为空时由 fit_body 硬截断原文兜底。
    """
    text = (text or "").strip()
    if not text:
        return ""
    # 1. 彻底清除推理模型的显式思考标签（CoT / 流口水）
    text = re.sub(r"<(?:think|thought|reasoning)>.*?</(?:think|thought|reasoning)>", "", text, flags=re.S | re.IGNORECASE)
    if re.search(r"</(?:think|thought|reasoning)>", text, flags=re.IGNORECASE):
        text = re.split(r"</(?:think|thought|reasoning)>", text, flags=re.IGNORECASE)[-1]
    if re.search(r"<(?:think|thought|reasoning)>", text, flags=re.IGNORECASE) and "<summary>" not in text:
        text = re.split(r"<(?:think|thought|reasoning)>", text, flags=re.IGNORECASE)[0]
    text = text.strip()
    if not text:
        return ""

    body = ""
    # 2. 定位摘要标签：若存在闭标签，取闭标签前最后一个开标签的内容；若未闭合，取最后一个开标签之后
    if "</summary>" in text:
        end_pos = text.rfind("</summary>")
        start_pos = text.rfind("<summary>", 0, end_pos)
        if start_pos != -1:
            body = text[start_pos + len("<summary>"):end_pos].strip()
        else:
            body = text[:end_pos].strip()
    elif "<summary>" in text:
        body = text.rsplit("<summary>", 1)[-1].strip()

    if body.startswith("```"):
        fence = re.fullmatch(r"```[^\n]*\n(.*?)(?:\n```)?\s*", body, re.S)
        if fence:
            body = fence.group(1).strip()

    # 3. 对提取出的 body 二次清洗独白与草稿前言
    if body:
        cleaned_body = _strip_monologue(body)
        if len(cleaned_body) >= 8:
            return cleaned_body

    # 4. 没标签或标签内剥完不足 8 字时的兜底：全文去独白（为空时由 fit_body 硬截断原文）
    return _strip_monologue(text)


def llm_summarize(text: str, cfg: dict | None = None) -> str:
    """调 OpenAI 兼容 chat/completions 把正文浓缩成 ≤max_chars 字战报摘要。

    任何失败（网络/非 0 状态码/空返回/剥完无正文）都返回空串，由 fit_body
    退化硬截断；只落打码后的日志，绝不抛。
    """
    cfg = cfg or llm_config()
    if not cfg["enabled"]:
        return ""
    payload = {
        "model": cfg["model"],
        # 中文 1 字 ≈ 1-2 token；推理/碎碎念模型先烧掉一段预算才轮到摘要，放宽至 6 倍且保底 2048 防截断
        "max_tokens": max(cfg["max_chars"] * 6, 2048),
        "temperature": 0.2,
        "messages": [
            {"role": "system",
             "content": "你是任务战报摘要器。把助手冗长的收尾汇报浓缩成不超过"
                        f"{cfg['max_chars']}字的中文摘要：保留结论、关键改动、"
                        "文件/命令名与数字，删去寒暄、过程性叙述与重复内容，"
                        "用 markdown 列表或短句。直接输出最终结果，严禁输出任何思考过程、"
                        "字数统计、分析说明或草稿。最终摘要必须严格用 <summary> 与 </summary> "
                        "标签包裹，标签内部只允许包含摘要正文本身，不要包含任何独白或草稿。"},
            {"role": "user", "content": text[:LLM_INPUT_MAX_CHARS]},
        ],
    }
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read())
        choice = (data.get("choices") or [{}])[0]
        content = str(((choice.get("message") or {}).get("content") or ""))
        if choice.get("finish_reason") == "length":
            log("llm summarize truncated (finish_reason=length)")
        return _extract_summary(content)
    except Exception as exc:  # fail-open：总结失败不拖发卡
        log(f"llm summarize failed (falling back to clip): {redact(exc)}")
        return ""


def fetch_models(base_url: str = "", api_key: str = "", timeout: float = 15) -> list[dict]:
    """从 OpenAI 兼容的 /models 接口拉取可用模型列表。

    返回 [{"id": str, "owned_by": str, "context_window": int | None}] 列表。
    调用失败时抛出带有打码信息的 RuntimeError。
    """
    cfg = llm_config()
    burl = (base_url.strip() if base_url is not None and base_url.strip() else (cfg.get("base_url") or DEFAULT_LLM_BASE_URL)).rstrip("/")
    key = api_key.strip() if api_key and api_key.strip() else (cfg.get("api_key") or "")
    if not burl:
        raise RuntimeError("未配置模型服务地址 (base_url)")
    url = f"{burl}/models"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", "replace")
        msg = raw_err[:300]
        try:
            err_json = json.loads(raw_err)
            if isinstance(err_json, dict) and "error" in err_json:
                e_obj = err_json["error"]
                if isinstance(e_obj, dict) and "message" in e_obj:
                    msg = str(e_obj["message"])
                elif isinstance(e_obj, str):
                    msg = e_obj
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {redact(msg)}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接模型服务: {redact(e.reason)}") from None
    except Exception as exc:
        raise RuntimeError(f"请求失败: {redact(exc)}") from None

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError(f"模型列表解析失败: {redact(exc)}") from None

    raw_list = []
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            raw_list = data["data"]
        elif isinstance(data.get("models"), list):
            raw_list = data["models"]
    elif isinstance(data, list):
        raw_list = data

    models = []
    seen = set()
    for item in raw_list:
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            owned = str(item.get("owned_by") or "").strip()
            ctx = (item.get("context_length") or item.get("context_window")
                   or (item.get("capabilities") or {}).get("contextWindow"))
            ctx_int = None
            if isinstance(ctx, (int, float)) and int(ctx) > 0:
                ctx_int = int(ctx)
            elif isinstance(ctx, str) and ctx.isdigit() and int(ctx) > 0:
                ctx_int = int(ctx)
            models.append({"id": mid, "owned_by": owned, "context_window": ctx_int})
        elif isinstance(item, str) and item.strip() and item.strip() not in seen:
            mid = item.strip()
            seen.add(mid)
            models.append({"id": mid, "owned_by": "", "context_window": None})

    models.sort(key=lambda m: m["id"].lower())
    return models


def fit_body(body: str, limit: int = BODY_MAX_BYTES, unit: str = "bytes",
             allow_summarize: bool = True) -> str:
    """正文按总结口径加工：够短原样，超阈值先 LLM 总结，未启用/失败再按上限兜底。

    触发条件（启用总结时）：正文超过 min_chars（默认 300 字）或超渠道上限——
    中等长度的冗长汇报也浓缩成战报摘要，不再逐字镜像；低于阈值保持原样，
    不为短正文多花一次 LLM 调用。min_chars=0 回到「只在超上限才总结」的旧口径。

    allow_summarize=False 用于 dry-run/预览（hook 前台同步路径）：绝不在这条路上
    发起 LLM 网络调用。
    """
    body = body or ""
    cfg = llm_config()
    over_limit = _size(body, unit) > limit
    if allow_summarize and cfg["enabled"]:
        over_threshold = cfg["min_chars"] > 0 and len(body) > cfg["min_chars"]
        if over_threshold or over_limit:
            summary = llm_summarize(body, cfg)
            if summary:
                return clip(summary, limit, unit)
    return clip(body, limit, unit)


# ── 统一卡片模型（对齐 fry-cards 家族规范）──────────────────────────────────────

DEFAULT_FOOTER_FIELDS = ("project", "model", "thinking", "tools", "context", "tokens", "elapsed")
# 统一面板头字段：卡片标题已含「项目 · 宿主」，面板头默认不再重复项目名
DEFAULT_PANEL_FIELDS = ("model", "thinking", "tools", "context", "tokens", "elapsed")
_LOCALES = ["zh_cn", "en_us"]
_ASSETS_ROOT = Path(__file__).resolve().parent / "assets"
_ASSETS_VENDOR_DIR = _ASSETS_ROOT / "vendor-logos"
_ASSETS_AGENT_DIR = _ASSETS_ROOT / "agent-logos"

# 厂商 Logo 匹配规则与官方模型名映射（与 hermes-fry-cards / claw-fry-cards 保持一致）
VENDOR_LOGO_RULES: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek.png"),
    ("qwen", "qwen.png"),
    ("zhipu", "zhipu.png"),
    ("glm", "zhipu.png"),
    ("zcode", "zhipu.png"),
    ("chatglm", "zhipu.png"),
    ("claude", "claude.png"),
    ("anthropic", "claude.png"),
    ("gemini", "gemini.png"),
    ("kimi", "kimi.png"),
    ("moonshot", "moonshot.png"),
    ("minimax", "minimax.png"),
    ("doubao", "doubao.png"),
    ("hunyuan", "hunyuan.png"),
    ("gpt", "openai.png"),
    ("openai", "openai.png"),
    ("step", "stepfun.png"),
    ("hy4", "hunyuan.png"),
    ("codebuddy", "codebuddy.png"),
    ("grok", "grok.png"),
    ("longcat", "longcat.png"),
    ("mimo", "xiaomimimo.png"),
    ("dots", "dotsstudio.png"),
    ("muse", "muse.png"),
    ("ling", "ling.png"),
    ("upstage", "upstage.png"),
)

_FAMILY_OFFICIAL: dict[str, str] = {
    "hy4": "Hy4",
    "hunyuan": "Hunyuan",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "glm": "GLM",
    "chatglm": "ChatGLM",
    "zhipu": "Zhipu",
    "zcode": "ZCode",
    "claude": "Claude",
    "anthropic": "Claude",
    "gemini": "Gemini",
    "kimi": "Kimi",
    "moonshot": "Moonshot",
    "minimax": "MiniMax",
    "doubao": "Doubao",
    "gpt": "GPT",
    "openai": "OpenAI",
    "step": "Step",
    "codebuddy": "CodeBuddy",
    "grok": "Grok",
    "longcat": "LongCat",
    "mimo": "MiMo",
    "dots": "Dots",
    "muse": "Muse",
    "ling": "Ling",
    "upstage": "Upstage",
}

_HOST_LABELS = {
    "claude-code": "Claude Code",
    "claude": "Claude Code",
    "codex": "Codex",
    "zcode": "ZCode",
    "kimi-code": "kimi-code",
    "kimi": "kimi-code",
    "antigravity": "Antigravity",
    "agy": "Antigravity",
}

# 宿主（智能体）Logo 资产名：模型未命中厂商家族时的兜底图标。
# 文件名与 vendor-logos/ 严格不重名——上传缓存键只用文件名，重名会串图。
AGENT_LOGO_FILES: dict[str, str] = {
    "claude-code": "claude-code.png",
    "claude": "claude-code.png",
    "codex": "codex.png",
    "zcode": "zcode.png",
    "kimi-code": "kimi-code.png",
    "kimi": "kimi-code.png",
    "antigravity": "antigravity.png",
    "agy": "antigravity.png",
}


def _split_host_model(model: str) -> tuple[str, str]:
    """拆分 'claude-code · tencent/hy4-flash:free' → ('claude-code', 'tencent/hy4-flash:free')。"""
    s = (model or "").strip()
    if " · " in s:
        prefix, rest = s.split(" · ", 1)
        return prefix.strip(), rest.strip()
    return "", s


def resolve_agent_logo_asset(model: str, host: str = "") -> str | None:
    """宿主/智能体 Logo 文件名（agent-logos/）；认不出宿主返回 None。

    宿主判定优先级：显式 `host` 参数 > 模型串 ' · ' 前的宿主前缀
    （适配器拼的就是「宿主 · 模型」）。与 `resolve_vendor_logo_asset` 分文取齐，
    调用方按「厂商命中优先、未命中才兜底」的顺序使用。
    """
    prefix, _ = _split_host_model(model)
    for slug in (host, prefix):
        name = AGENT_LOGO_FILES.get((slug or "").strip().lower())
        if name:
            return name
    return None


# 路由别名（池化模型）识别：这些名字是网关侧的「路由入口」，不是真实厂商模型。
# 它们往往自带厂商词（`openai/chat-mode-pool`、`qwork-claude-chat-mode-pool`），
# 若按厂商规则匹配就会出 OpenAI / Claude 的厂商 logo —— 而真正执行的是卡片宿主
# 智能体（Claude Code / Codex …）。所以别名必须先于厂商判定被认出来。
ALIAS_MODEL_MARKERS: tuple[str, ...] = (
    "chat-mode-pool",
    "chat_mode_pool",
    "chatmodepool",
    "mode-pool",
)


def is_alias_model(model: str) -> bool:
    """模型名是否是路由别名（池化入口），而不是具体厂商模型。

    只看 `宿主 · ` 之后的原始模型段，避免把宿主名前缀误判成别名。
    """
    _, raw_model = _split_host_model(model)
    lower = (raw_model or "").strip().lower()
    if not lower:
        return False
    if lower.startswith("alias:"):
        return True
    return any(marker in lower for marker in ALIAS_MODEL_MARKERS)


def resolve_alias_route_asset(model: str, host: str = "") -> str | None:
    """别名模型路由到的智能体 logo；解析不出路由目标返回 None。

    优先级：
    1. 模型串自带的路由目标标记 `alias:claude-code` / `→ codex`（网关或配置显式给出）；
    2. 显式 `host` 参数（发卡时适配器上报的宿主，也就是真实执行者）；
    3. 模型串 `宿主 · ` 前缀。
    拿不到任何一层时才返回 None，由调用方回退厂商 logo。
    """
    _, raw_model = _split_host_model(model)
    lower = (raw_model or "").strip().lower()
    if lower.startswith("alias:"):
        target = lower.split(":", 1)[1].strip()
        name = AGENT_LOGO_FILES.get(target)
        if name:
            return name
    if "→" in raw_model or "->" in raw_model:
        tail = re.split(r"→|->", raw_model, maxsplit=1)[-1].strip().lower()
        name = AGENT_LOGO_FILES.get(tail)
        if name:
            return name
    return resolve_agent_logo_asset(model, host)


def resolve_logo_asset(model: str, host: str = "") -> str | None:
    """面板头 Logo 资产名。

    顺序：路由别名 → 它路由到的智能体图；否则厂商家族图；都未命中再兜底宿主图。
    别名若解析不出路由目标，回落厂商图（`openai/chat-mode-pool` → openai.png），
    保证「认不出路由目标也不会空着」。
    """
    if is_alias_model(model):
        routed = resolve_alias_route_asset(model, host)
        if routed:
            return routed
    return resolve_vendor_logo_asset(model) or resolve_agent_logo_asset(model, host)


def logo_asset_path(asset: str) -> Path | None:
    """资产名 → 本地 PNG 路径（厂商目录优先，其次智能体目录）；都不存在返回 None。"""
    name = (asset or "").strip()
    if not name or "/" in name or "\\" in name or name != Path(name).name:
        return None
    for base in (_ASSETS_VENDOR_DIR, _ASSETS_AGENT_DIR):
        p = base / name
        if p.is_file():
            return p
    return None


def resolve_vendor_logo_asset(model: str) -> str | None:
    """模型名 → 本地 vendor-logos 文件名；未命中返回 None。"""
    _, raw_model = _split_host_model(model)
    lower = raw_model.lower()
    for needle, file in VENDOR_LOGO_RULES:
        if needle in lower:
            return file
    return None


def resolve_route_label(model: str, host: str = "") -> str | None:
    """别名模型路由到的智能体展示名（`Claude Code` / `Codex` …）；解析不出返回 None。

    与 `resolve_alias_route_asset` 同一套优先级，只是给展示名用。
    """
    asset = resolve_alias_route_asset(model, host)
    if not asset:
        return None
    for slug, name in AGENT_LOGO_FILES.items():
        if name == asset:
            return _HOST_LABELS.get(slug, slug)
    return None


def resolve_vendor_display_name(model: str) -> str | None:
    """代理拼接名 → 官方模型展示名（与 hermes/claw-fry-cards 同口径）；未命中返回 None。"""
    _, raw_model = _split_host_model(model)
    if not raw_model:
        return None
    file = resolve_vendor_logo_asset(raw_model)
    if not file:
        return None
    lower = raw_model.lower()
    needle = next((n for n, f in VENDOR_LOGO_RULES if f == file and n in lower), None)
    if needle is None:
        return None
    official = _FAMILY_OFFICIAL.get(needle, needle.capitalize())
    base = raw_model.strip().split("/")[-1].split(":")[0].strip()
    idx = base.lower().find(needle)
    if idx < 0:
        return official
    tail = base[idx + len(needle):].lstrip("-_").strip()
    return f"{official} {tail}".strip() or official


def format_panel_model(model: str, host_prefix: bool | None = None,
                       host: str = "") -> str:
    """面板标题里的模型展示名：默认不重复宿主前缀，只留模型官方名。

    卡片标题已含「项目 · 宿主」，面板头再带一遍宿主就是同一张卡说三遍；
    host_prefix 为真（FEIGE_PANEL_HOST_PREFIX / 设置 panel_host_prefix）时才还原
    「claude-code · Qwen 4-max」旧口径。显式传参优先于配置。

    路由别名（`chat-mode-pool` 一类）不按字面厂商展示——它真正跑在哪个智能体上
    才是这张卡该说的：解析出路由目标就显示目标名（`Claude Code`），解析不出
    才退回厂商官方名。
    """
    prefix_allowed = panel_host_prefix_enabled() if host_prefix is None else bool(host_prefix)
    host_prefix_text, raw_model = _split_host_model(model)
    if not raw_model:
        # 只有宿主没有模型（'claude-code'）：关前缀时留空让调用方回落标题兜底
        return host_prefix_text if prefix_allowed else ""
    if is_alias_model(model):
        # 整串传下去：宿主前缀本身就是路由目标，别只拿 raw 段丢了这个信息
        shown = (resolve_route_label(model, host) or resolve_vendor_display_name(raw_model)
                 or raw_model.split("/")[-1])
    else:
        shown = resolve_vendor_display_name(raw_model) or raw_model.split("/")[-1]
    return f"{host_prefix_text} · {shown}" if (host_prefix_text and prefix_allowed) else shown


def _bool_setting(env_name: str, default: bool = False) -> bool:
    raw = _env(env_name)
    if not raw:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def interactive_enabled() -> bool:
    """feishu-cardkit 交互式卡片（表单/快捷按钮）是否开启（默认开启）。"""
    return _bool_setting("FEIGE_INTERACTIVE", default=True)


def ws_enabled() -> bool:
    """飞书长连接回调监听开关（默认关，需在 Studio 或环境变量开启）。"""
    return _bool_setting("FEIGE_WS_ENABLED", default=False)


def vendor_logo_enabled() -> bool:
    """是否展示面板头 Logo（默认开启）。

    管的是「厂商图 + 未命中时的智能体兜底图」整条链路：关掉后两者都不出，
    面板头回到只有右侧折叠箭头的旧观感。
    """
    return _bool_setting("FEIGE_VENDOR_LOGO", default=True)


def panel_expanded_default() -> bool:
    """底部统一折叠面板是否默认展开（默认折叠）。"""
    return _bool_setting("FEIGE_PANEL_EXPANDED", default=False)


def panel_host_prefix_enabled() -> bool:
    """面板头模型名是否带宿主前缀（默认关：标题已含宿主，不再重复）。"""
    return _bool_setting("FEIGE_PANEL_HOST_PREFIX", default=False)


def allow_users_list() -> list[str]:
    """操作人白名单（open_id / user_id），逗号/分号/换行分隔；空列表表示不限。"""
    raw = _env("FEIGE_ALLOW_USERS")
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        uid = part.strip()
        if uid and uid not in out:
            out.append(uid)
    return out


def resume_timeout_seconds() -> float:
    raw = _env("FEIGE_RESUME_TIMEOUT")
    try:
        return max(30.0, min(1800.0, float(raw))) if raw else 300.0
    except ValueError:
        return 300.0


def sessions_file_path(env: dict | None = None) -> str:
    """本地交互会话映射文件路径：FEIGE_SESSIONS_FILE > 设置文件 > ~/.feige-sessions.json。"""
    if env is not None:
        override = str(env.get("FEIGE_SESSIONS_FILE") or "").strip()
    else:
        override = _env("FEIGE_SESSIONS_FILE")
    return override or str(Path.home() / ".feige-sessions.json")


def _field_order(env_name: str, settings_key: str,
                 defaults: tuple[str, ...],
                 fields: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    """字段顺序解析（面板头/脚注共用）：显式传入 > 环境变量 > 设置文件 > 默认。

    空列表是合法配置（全部隐藏），只有键不存在才落默认。
    """
    if fields is not None:
        return tuple(fields)
    cfg_fields = None
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        cfg_fields = [x.strip() for x in env_val.split(",") if x.strip()]
    if cfg_fields is None:
        saved = load_settings().get(settings_key)
        if isinstance(saved, list):
            cfg_fields = [str(x).strip() for x in saved if str(x).strip()]
        elif isinstance(saved, str) and saved.strip():
            cfg_fields = [str(x).strip() for x in saved.split(",") if str(x).strip()]
    return tuple(cfg_fields) if cfg_fields is not None else defaults


def _footer_field_order(fields: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    """脚注字段：FEIGE_FOOTER_FIELDS > 设置 footer_fields > 全字段默认。"""
    return _field_order("FEIGE_FOOTER_FIELDS", "footer_fields", DEFAULT_FOOTER_FIELDS, fields)


def _panel_field_order(fields: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    """统一面板头字段：FEIGE_PANEL_FIELDS > 设置 panel_fields > 默认（无项目名）。

    独立于脚注配置：标题已含「项目 · 宿主」，面板头默认不再重复；
    旧配置只有 footer_fields 时不迁移——面板走新默认，脚注保持旧口径。
    """
    return _field_order("FEIGE_PANEL_FIELDS", "panel_fields", DEFAULT_PANEL_FIELDS, fields)


def footer_enabled() -> bool:
    """完成态卡底部脚注行（hr + 统计行）显隐开关（默认关）。

    面板头已承载同一套统计，脚注默认不再重复；只作用于飞书卡片渠道，
    webhook/钉钉/Telegram 静态版式与预览 JSON 不受此开关影响。
    """
    return _bool_setting("FEIGE_FOOTER_ENABLED", default=False)


_CONTEXT_STYLES = ("text", "bar", "text_bar")
# 旧口径兼容：2.0.x 试用的 plain 等价于家族的 text
_CONTEXT_STYLE_ALIASES = {"plain": "text"}


def normalize_context_style(value) -> str:
    """把任意输入（含旧口径 plain、大小写/空格噪声）归一到三档之一，认不出返回空串。"""
    v = str(value or "").strip().lower()
    v = _CONTEXT_STYLE_ALIASES.get(v, v)
    return v if v in _CONTEXT_STYLES else ""


def context_style(env: dict | None = None, settings: dict | None = None) -> str:
    """统一面板头上下文水位样式（三档，口径对齐 claw / hermes-fry-cards）：

    - ``text``：纯文本 ``165.4k/1.0m (17%)``
    - ``bar``：纯进度条 ``[█▓▒░] 17%``
    - ``text_bar``：文本 + 进度条 ``165.4k/1.0m [█▓▒░] 17%``（默认）

    优先级：FEIGE_CONTEXT_STYLE > ~/.feige-settings.json 的 context_style > text_bar；
    非法值回落默认，旧口径 ``plain`` 等价 ``text``。默认取 text_bar——2.0.0 的面板头
    本来就是「文本 + 密度条」，旧配置不写键也落回同一呈现。
    只作用于飞书卡片统一面板头；脚注与钉钉/Telegram 静态版式保持原样口径。
    """
    env = env if env is not None else os.environ
    v = normalize_context_style(env.get("FEIGE_CONTEXT_STYLE"))
    if v:
        return v
    v = normalize_context_style((load_settings() if settings is None else settings).get("context_style"))
    return v or "text_bar"


def _context_progress_bar(pct: float, width: int = 8) -> str:
    """生成渐变密度条 █▓▒░（对齐 hermes/claw-fry-cards）。"""
    pct = max(0.0, min(float(pct), 100.0))
    n = pct / 100.0 * width
    cells: list[str] = []
    for i in range(width):
        pos = i + 0.5
        if pos <= n:
            if n - pos >= 0.5:
                cells.append("█")
            elif n - pos >= 0.25:
                cells.append("▓")
            else:
                cells.append("▒")
        elif pos - 1 <= n:
            frac = n - (pos - 1)
            if frac > 0.66:
                cells.append("▓")
            elif frac > 0.33:
                cells.append("▒")
            else:
                cells.append("░")
        else:
            cells.append("░")
    return "".join(cells)


def _format_panel_context(context_str: str, style: str = "") -> str:
    """将 '153.6k/1.0m (15%)' 或 '42%' 按面板头样式渲染（三档，对齐 claw / hermes）。

    style 为空时读设置（context_style()）：
    - text：上游原样 ``153.6k/1.0m (15%)``
    - bar：纯进度条 ``[█▓░░░░░░] 15%``
    - text_bar（默认）：文本 + 进度条 ``153.6k/1.0m [█▓░░░░░░] 15%``
    不带文本的 '42%' 在 text 档保持 '42%'，bar / text_bar 档给进度条。
    """
    s = (context_str or "").strip()
    if not s:
        return ""
    mode = normalize_context_style(style) or context_style()
    m = re.match(r"^(.+?)\s*\((\d+(?:\.\d+)?)%\)$", s)
    if m:
        ratio, pct = m.group(1).strip(), float(m.group(2))
        if mode == "text":
            return s
        if mode == "bar":
            return f"[{_context_progress_bar(pct)}] {pct:.0f}%"
        return f"{ratio} [{_context_progress_bar(pct)}] {pct:.0f}%"
    m_pct = re.match(r"^(\d+(?:\.\d+)?)%$", s)
    if m_pct:
        pct = float(m_pct.group(1))
        if mode == "text":
            return s
        return f"[{_context_progress_bar(pct)}] {pct:.0f}%"
    return s


def stats_footer(stats: dict | None, fields: list[str] | tuple[str, ...] | None = None) -> str:
    """拼装统计脚注行：「📦 项目 · 模型 · 💭思考 · 🔧工具 · 上下文 · 🎫输出 token · ⏱️耗时」。

    约定与家族统一面板口径一致；空字段自动省略。
    可按 fields 列表/元组自定义字段与出现顺序。若未传，则读取设置中的 footer_fields 或默认顺序。
    """
    if not stats:
        return ""
    fields = _footer_field_order(fields)

    field_map = {}
    project = str(stats.get("project") or "").strip()
    if project:
        field_map["project"] = f"📦 {project}"
    model = str(stats.get("model") or "").strip()
    if model:
        field_map["model"] = model.split("/")[-1]  # 只留模型名末段，渠道前缀太吵
    thinking = stats.get("thinking")
    if thinking is not None and str(thinking).strip() and str(thinking) != "0":
        field_map["thinking"] = f"💭{thinking}"
    tools = stats.get("tools")
    if tools is not None and str(tools).strip() and str(tools) != "0":
        field_map["tools"] = f"🔧{tools}"
    context = str(stats.get("context") or "").strip()
    if context:
        field_map["context"] = context  # 形如 "42%" 或 "86.5k/200.0k (43%)"，上游已格式化
    tokens = stats.get("tokens")
    if tokens is not None and str(tokens).strip():
        try:
            token_count = int(tokens)
        except (TypeError, ValueError):
            token_label = str(tokens).strip()  # 允许上游传入已格式化的数量
        else:
            token_label = compact_count(token_count) if token_count > 0 else ""
        if token_label:
            field_map["tokens"] = f"🎫 {token_label}"
    elapsed = str(stats.get("elapsed") or "").strip()
    if elapsed:
        field_map["elapsed"] = f"⏱️ {elapsed}"

    parts = []
    for f in fields:
        if f in field_map:
            parts.append(field_map[f])
    return " · ".join(parts)


def compact_count(value: int) -> str:
    """数量紧凑格式，与 ZCode bridge 一致：14,300 → 14.3k。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _build_panel_header_text(stats: dict | None, logo_image_key: str = "",
                             fields: list[str] | tuple[str, ...] | None = None,
                             context_style: str = "",
                             panel_host_prefix: bool | None = None,
                             host: str = "") -> str:
    """统一面板标题文本（对齐 fry-cards）：不添加多余 emoji，保持图标干净（未传 custom_icon 时仅展示统计标题）。

    字段与顺序走独立的面板配置（panel_fields / FEIGE_PANEL_FIELDS），默认不含
    项目名——卡片标题已含「项目 · 宿主」，同一张卡不重复三遍。
    `host` 是发卡时上报的宿主（真实执行者），别名模型靠它解析路由目标。
    """
    parts: list[str] = []
    if stats:
        order = _panel_field_order(fields)
        fmap: dict[str, str] = {}
        project = str(stats.get("project") or "").strip()
        if project:
            fmap["project"] = f"📦 {project}"
        model = str(stats.get("model") or "").strip()
        if model:
            fmap["model"] = format_panel_model(model, host_prefix=panel_host_prefix, host=host)
        thinking = stats.get("thinking")
        if thinking is not None and str(thinking).strip():
            try:
                fmap["thinking"] = f"💭{int(thinking)}"
            except (TypeError, ValueError):
                fmap["thinking"] = f"💭{thinking}"
        tools = stats.get("tools")
        if tools is not None and str(tools).strip():
            try:
                fmap["tools"] = f"🔧{int(tools)}"
            except (TypeError, ValueError):
                fmap["tools"] = f"🔧{tools}"
        ctx = str(stats.get("context") or "").strip()
        if ctx:
            fmap["context"] = _format_panel_context(ctx, context_style)
        tokens = stats.get("tokens")
        if tokens is not None and str(tokens).strip():
            try:
                tc = int(tokens)
                tlabel = compact_count(tc) if tc > 0 else ""
            except (TypeError, ValueError):
                tlabel = str(tokens).strip()
            if tlabel:
                fmap["tokens"] = f"🎫 {tlabel}"
        elapsed = str(stats.get("elapsed") or "").strip()
        if elapsed:
            fmap["elapsed"] = f"⏱️ {elapsed}"
        for k in order:
            if k in fmap and fmap[k]:
                parts.append(fmap[k])
    if len(parts) <= (1 if not logo_image_key else 0):
        parts.append("会话详情与统计")
    return " · ".join(parts)


def _build_unified_panel(
    *,
    stats: dict | None = None,
    status: str = "ok",
    detail_body: str = "",
    tool_summary: str = "",
    host: str = "",
    ctx_id: str = "",
    logo_image_key: str = "",
    expanded: bool = False,
    context_style: str = "",
    panel_host_prefix: bool | None = None,
) -> dict | None:
    """构建家族底部统一折叠面板（collapsible_panel），承载详细正文、工具摘要与指标。"""
    panel_children: list[dict] = []
    detail = (detail_body or "").strip()
    if detail:
        clipped_detail = clip(detail, DETAIL_PANEL_MAX_BYTES)
        panel_children.append({
            "tag": "markdown",
            "content": f"**详细摘要 / 原文**\n\n{clipped_detail}",
            "text_size": "notation",
        })
    tsum = (tool_summary or "").strip()
    if tsum:
        panel_children.append({
            "tag": "markdown",
            "content": f"🔧 **工具调用**：{tsum}",
            "text_size": "notation",
        })

    meta_parts: list[str] = []
    if host:
        meta_parts.append(f"宿主：{_HOST_LABELS.get(host.lower(), host)}")
    raw_model = str((stats or {}).get("model") or "").strip()
    if raw_model:
        # 别名模型展示路由目标，同时把原名留在括号里，方便回查网关侧是哪条池
        if is_alias_model(raw_model):
            route = resolve_route_label(raw_model, host) or resolve_vendor_display_name(raw_model)
            if route and route not in raw_model:
                meta_parts.append(f"模型：{route}（`{raw_model}`）")
            else:
                meta_parts.append(f"模型：`{raw_model}`")
        else:
            official = resolve_vendor_display_name(raw_model)
            if official and official not in raw_model:
                meta_parts.append(f"模型：{official}（`{raw_model}`）")
            else:
                meta_parts.append(f"模型：`{raw_model}`")
    if ctx_id:
        meta_parts.append(f"上下文：`#{ctx_id[:8]}`")
    if meta_parts:
        panel_children.append({
            "tag": "markdown",
            "content": " · ".join(meta_parts),
            "text_size": "notation",
        })

    if not panel_children and not stats:
        return None
    if not panel_children:
        panel_children.append({
            "tag": "markdown",
            "content": "暂无更多展开详情。",
            "text_size": "notation",
        })

    header_title = _build_panel_header_text(stats, logo_image_key=logo_image_key,
                                            context_style=context_style,
                                            panel_host_prefix=panel_host_prefix,
                                            host=host)
    header_obj: dict = {
        "title": {
            "tag": "plain_text",
            "content": header_title,
            "i18n_content": {"zh_cn": header_title, "en_us": header_title},
        },
        "vertical_align": "center",
        "text_color": "grey",
        "text_size": "notation",
    }
    if logo_image_key:
        header_obj["icon"] = {
            "tag": "custom_icon",
            "img_key": logo_image_key,
            "size": "16px 16px",
        }
        header_obj["icon_position"] = "left"
        header_obj["icon_expanded_angle"] = 0
    else:
        header_obj["icon"] = {
            "tag": "standard_icon",
            "token": "down-small-ccm_outlined",
            "color": "grey",
            "size": "16px 16px",
        }
        header_obj["icon_position"] = "right"
        header_obj["icon_expanded_angle"] = -180

    border_color = PANEL_BORDER_COLOR.get(status, "green")
    return {
        "tag": "collapsible_panel",
        "element_id": UNIFIED_PANEL_ELEMENT_ID,
        "expanded": bool(expanded),
        "header": header_obj,
        "border": {"color": border_color, "corner_radius": "5px"},
        "vertical_spacing": "4px",
        "padding": "8px 8px 8px 8px",
        "elements": panel_children,
    }


def _build_interactive_elements(ctx_id: str, host: str = "", resumable: bool = True) -> list[dict]:
    """构建 CardKit 2.0 交互区：追问表单 + 防误触快捷按钮（不支持续跑的宿主给清晰提示）。"""
    if not ctx_id:
        return []
    host_label = _HOST_LABELS.get((host or "").lower(), host or "当前宿主")
    if not resumable:
        return [
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_align": "left",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "name": f"feige_unsupported_{ctx_id}",
                                "text": {"tag": "plain_text", "content": "ℹ️ 续跑说明"},
                                "type": "default",
                                "value": {"feige_action": "unsupported", "ctx_id": ctx_id},
                            }
                        ],
                    }
                ],
            },
            {
                "tag": "markdown",
                "content": f"<font color='grey'>💡 {host_label} 暂不支持远程无头续跑，请在本地终端 / IDE 中继续会话。</font>",
                "text_size": "notation",
            },
        ]

    return [
        {
            "tag": "form",
            "name": f"feige_form_{ctx_id}",
            "elements": [
                {
                    "tag": "input",
                    "name": "followup_prompt",
                    "input_type": "multiline_text",
                    "rows": 2,
                    "auto_resize": True,
                    "max_rows": 5,
                    "width": "fill",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "输入追问或继续指令（仅校验通过的操作人可触发续跑）…",
                    },
                },
                {
                    "tag": "button",
                    "name": f"feige_submit_{ctx_id}",
                    "text": {"tag": "plain_text", "content": "📤 发送追问"},
                    "type": "primary",
                    "form_action_type": "submit",
                    "value": {"feige_action": "followup", "ctx_id": ctx_id},
                },
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_align": "left",
            "columns": [
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [
                        {
                            "tag": "button",
                            "name": f"feige_continue_{ctx_id}",
                            "text": {"tag": "plain_text", "content": "▶️ 继续执行"},
                            "type": "default",
                            "value": {"feige_action": "continue", "ctx_id": ctx_id},
                            "confirm": {
                                "title": {"tag": "plain_text", "content": "确认继续执行？"},
                                "text": {"tag": "plain_text", "content": f"将调用 {host_label} 基于原会话继续执行未完成的任务。"},
                            },
                        }
                    ],
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [
                        {
                            "tag": "button",
                            "name": f"feige_tests_{ctx_id}",
                            "text": {"tag": "plain_text", "content": "🧪 跑测试核验"},
                            "type": "default",
                            "value": {"feige_action": "run_tests", "ctx_id": ctx_id},
                            "confirm": {
                                "title": {"tag": "plain_text", "content": "确认运行测试？"},
                                "text": {"tag": "plain_text", "content": f"将调用 {host_label} 运行项目测试并回传结果。"},
                            },
                        }
                    ],
                },
            ],
        },
    ]


def build_card(title: str, body: str, stats: dict | None = None,
               status: str = "ok", streaming: bool = False,
               allow_summarize: bool = True,
               *,
               detail_body: str = "",
               tool_summary: str = "",
               host: str = "",
               ctx_id: str = "",
               interactive: bool = False,
               resumable: bool = True,
               logo_image_key: str = "",
               panel_expanded: bool | None = None,
               safe_bytes: int | None = None,
               context_style: str = "",
               panel_host_prefix: bool | None = None) -> dict:
    """统一卡片模型 → 飞书 Card 2.0 JSON（对齐 fry-cards 家族规范）。

    - 标题按状态加前缀（🛠️/✅/❌），header 按状态着色（蓝/绿/红）；
    - 顶层正文保持精简战报（fit_body 处理）；streaming=True 时仅保留流式元素；
    - 完成态（ok/error）附带底部统一折叠面板（collapsible_panel）承载详细正文、
      工具摘要与厂商家族 Logo + 官方模型名；
    - interactive=True（feishu-cardkit）且有 ctx_id 时附加追问表单与防误触快捷按钮；
    - 统计脚注（notation）默认不再追加——面板头已承载同一套统计，同一张卡不出
      三遍；footer_enabled()（FEIGE_FOOTER_ENABLED / 设置 footer_enabled）显式
      打开才恢复 hr + 脚注行。静态渠道（钉钉/Telegram）不受此开关影响。
    """
    if status not in STATUS_TEMPLATE:
        raise ValueError(f"unknown status: {status!r} (expect running/ok/error)")
    full_title = f"{STATUS_PREFIX[status]} {title.strip() or '通知'}"

    config: dict = {
        "width_mode": "default",
        "update_multi": True,
        "locales": _LOCALES,
        "summary": {"content": full_title},
    }
    if streaming:
        config["streaming_mode"] = True
        config["streaming_config"] = {
            "print_frequency_ms": {"default": 15},
            "print_step": {"default": 1},
            "print_strategy": "fast",
        }

    raw_body = (body or "").strip()
    summary_body = fit_body(raw_body, allow_summarize=allow_summarize) or "（空内容）"
    element: dict = {
        "tag": "markdown",
        "content": summary_body,
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": "0px 0px 0px 0px",
    }
    if streaming:
        element["element_id"] = STREAMING_ELEMENT_ID
    elements: list[dict] = [element]

    footer = (stats_footer(stats)
              if status != "running" and footer_enabled() else "")
    if status != "running":
        eff_detail = (detail_body or "").strip()
        # 若顶层正文已被 LLM 总结或截断，且未显式传入 detail_body，则保留原文前缀供折叠面板展开阅读
        if not eff_detail and raw_body and raw_body != summary_body and len(raw_body) <= 8000:
            eff_detail = raw_body
        eff_expanded = panel_expanded_default() if panel_expanded is None else bool(panel_expanded)
        panel = _build_unified_panel(
            stats=stats,
            status=status,
            detail_body=eff_detail,
            tool_summary=tool_summary,
            host=host,
            ctx_id=ctx_id,
            logo_image_key=logo_image_key if vendor_logo_enabled() else "",
            expanded=eff_expanded,
            context_style=context_style,
            panel_host_prefix=panel_host_prefix,
        )
        if panel is not None:
            elements.append(panel)
        if interactive and ctx_id:
            elements.extend(_build_interactive_elements(ctx_id, host=host, resumable=resumable))
        if footer:
            # Card 2.0 没有 note 组件：脚注用 markdown + text_size:notation
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": footer, "text_size": "notation"})

    card = {
        "schema": "2.0",
        "config": config,
        "header": {
            "title": {"tag": "plain_text", "content": full_title},
            "template": STATUS_TEMPLATE[status],
        },
        "body": {"elements": elements},
    }

    # 整卡体积安全钳制（防 webhook 20KB / CardKit 30KB 超限）
    budget = safe_bytes or (CARD_CARDKIT_SAFE_BYTES if interactive else CARD_WEBHOOK_SAFE_BYTES)
    if status != "running" and len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > budget:
        for el in elements:
            if isinstance(el, dict) and el.get("element_id") == UNIFIED_PANEL_ELEMENT_ID:
                sub = el.get("elements") or []
                el["elements"] = [c for c in sub if "**详细摘要 / 原文**" not in str(c.get("content", ""))]
                if not el["elements"]:
                    el["elements"] = [{"tag": "markdown", "content": "（详情因卡片体积限制已省略）", "text_size": "notation"}]
                break
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > budget:
            card["body"]["elements"] = [
                el for el in elements
                if not (isinstance(el, dict) and el.get("element_id") == UNIFIED_PANEL_ELEMENT_ID)
            ]
    return card


# ── 底层 HTTP ────────────────────────────────────────────────────────────────

def _post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 10) -> dict:
    """POST JSON，返回解析后的响应；HTTP 错误抛带打码 body 的 RuntimeError。

    回环地址绕过系统代理：HTTP_PROXY 配了「拦一切 http://」时，发往本机
    webhook/调试服务器的请求会被代理打断（502 等），loopback 一律直连。
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "::1", "localhost") or host.endswith(".localhost"):
        # ProxyHandler({}) = 空代理表，直连（urlopen 否则取 getproxies() 走系统代理）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = None
    try:
        open_url = opener.open if opener else urllib.request.urlopen
        with open_url(req, timeout=timeout) as resp:
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


_PROJECT_CHAT_PREFIX = "feishu_chat_id__"  # 设置文件里按项目覆盖的键前缀

def project_chat_id(project: str) -> str:
    """按项目覆盖接收群：设置文件 `"feishu_chat_id__<项目名>": "oc_xxx"`。

    回退链：环境变量 `FEISHU_CHAT_ID__<项目名>`（先取，供插件设置注入）
    > 设置文件 `feishu_chat_id__<项目名>` > default_chat_id()（全局默认群）。
    项目名为空时直接走默认群。

    这条链解决「一个宿主里不同项目发不同群但只想用一条全局配置」的场景：
    ChatID 只有在路由目标显式给了 chat_id 时才会被目标覆盖，没给就落到这里。
    命名用双下划线而不是 `项目.feishu_chat_id`——后者会被 load_settings 里的
    点号嵌套语义拆开，也便于在 Studio 表单里按前缀扫描。
    """
    name = (project or "").strip()
    if not name:
        return default_chat_id()
    env_key = f"FEISHU_CHAT_ID__{name}"
    value = (os.environ.get(env_key) or "").strip()
    if value:
        return value
    settings = load_settings()
    value = str(settings.get(_PROJECT_CHAT_PREFIX + name) or "").strip()
    if value:
        return value
    return default_chat_id()


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


_LOGO_CACHE: dict[str, str | None] = {}


def _logo_cache_path() -> Path:
    return Path(os.environ.get("FEIGE_LOGO_CACHE_FILE") or (Path.home() / ".feige-logo-cache.json"))


def upload_image_bytes(data: bytes, filename: str = "logo.png") -> str:
    """上传本地 PNG 图片到飞书换取 image_key（纯标准库 multipart/form-data）。"""
    boundary = f"----feige{int(time.time() * 1000)}{os.getpid()}"
    crlf = b"\r\n"
    parts = [
        b"--" + boundary.encode("ascii"),
        b'Content-Disposition: form-data; name="image_type"',
        b"",
        b"message",
        b"--" + boundary.encode("ascii"),
        f'Content-Disposition: form-data; name="image"; filename="{filename}"'.encode("utf-8"),
        b"Content-Type: image/png",
        b"",
        data,
        b"--" + boundary.encode("ascii") + b"--",
        b"",
    ]
    payload = crlf.join(parts)
    req = urllib.request.Request(
        app_api() + "/im/v1/images",
        data=payload,
        headers={
            "Authorization": f"Bearer {get_tenant_token()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read())
    key = str((res.get("data") or {}).get("image_key") or "").strip()
    if res.get("code") != 0 or not key:
        raise RuntimeError(f"upload image failed: code={res.get('code')} msg={redact(res.get('msg', ''))}")
    return key


def resolve_vendor_logo_image_key(model: str, host: str = "") -> str:
    """解析并缓存面板头 Logo 的飞书 image_key（仅 feishu-cardkit 使用，失败静默降级空串）。

    口径：命中厂商家族用厂商图；未命中（如代理池 / 自建网关的 `chat-mode-pool`
    这类认不出的模型名）兜底宿主/智能体图，面板头不再空着左侧。名字沿用厂商版
    以兼容既有调用与测试打桩。
    """
    if not vendor_logo_enabled():
        return ""
    asset = resolve_logo_asset(model, host)
    if not asset:
        return ""
    app_id = _env("FEISHU_APP_ID", "LARK_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET", "LARK_APP_SECRET")
    if not app_id or not app_secret:
        return ""
    img_path = logo_asset_path(asset)
    if img_path is None:
        return ""
    try:
        data = img_path.read_bytes()
    except OSError as exc:
        log(f"vendor logo read failed ({asset}): {redact(exc)}")
        return ""
    # 缓存键带内容摘要：资产像素换过（如纯白图改成白底可见版）必须重新上传，
    # 否则旧 image_key 会一直命中，修复在卡上永远看不见。
    cache_key = f"{app_base()}|{app_id}|{asset}|{hashlib.sha256(data).hexdigest()[:12]}"
    if cache_key in _LOGO_CACHE:
        return _LOGO_CACHE[cache_key] or ""
    cpath = _logo_cache_path()
    try:
        disk = json.loads(cpath.read_text(encoding="utf-8")) if cpath.is_file() else {}
    except (OSError, ValueError):
        disk = {}
    if isinstance(disk, dict) and isinstance(disk.get(cache_key), str) and disk[cache_key]:
        _LOGO_CACHE[cache_key] = disk[cache_key]
        return disk[cache_key]
    try:
        key = upload_image_bytes(data, filename=asset)
        _LOGO_CACHE[cache_key] = key
        try:
            if not isinstance(disk, dict):
                disk = {}
            disk[cache_key] = key
            cpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(cpath) + ".tmp"
            Path(tmp).write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, cpath)
        except OSError:
            pass
        return key
    except Exception as exc:
        log(f"vendor logo upload skipped ({asset}): {redact(exc)}")
        _LOGO_CACHE[cache_key] = None
        return ""


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


def update_card(card_id: str, card: dict, sequence: int) -> int:
    """全量更新已发送的 CardKit 卡片：PUT /cardkit/v1/cards/:card_id，返回下一个 sequence。"""
    r = call_app_api("PUT", f"/cardkit/v1/cards/{card_id}",
                     {"card": {"type": "card_json",
                               "data": json.dumps(card, ensure_ascii=False)},
                      "sequence": sequence})
    if r.get("code") != 0:
        raise RuntimeError(f"update card failed: code={r.get('code')} msg={redact(r.get('msg', ''))}")
    return sequence + 1


def seal_card(card_id: str, card: dict, sequence: int) -> int:
    """封卡：PUT 整卡（完成态卡片 JSON）→ PATCH settings 关流式（用 PUT 会 404）。

    返回下一个可用 sequence；关流式失败不致命，只落日志。
    """
    sequence = update_card(card_id, card, sequence)
    r = call_app_api("PATCH", f"/cardkit/v1/cards/{card_id}/settings",
                     {"settings": json.dumps({"streaming_mode": False}),
                      "sequence": sequence})
    if r.get("code") != 0:
        log(f"close streaming failed (non-fatal): {r.get('code')} {redact(r.get('msg', ''))}")
    return sequence + 1


def send_card(card_id: str, chat_id: str = "", reply_to_message_id: str = "") -> str:
    """按引用把卡片发到群（或回复原消息）：content = {"type": "card", "data": {"card_id": ...}}。"""
    content = {"type": "card", "data": {"card_id": card_id}}
    if reply_to_message_id:
        r = call_app_api("POST", f"/im/v1/messages/{reply_to_message_id}/reply",
                         {"msg_type": "interactive",
                          "content": json.dumps(content, ensure_ascii=False)})
    else:
        chat = chat_id or default_chat_id()
        if not chat:
            raise RuntimeError("no chat_id: pass --chat-id or set FEISHU_NOTIFY_CHAT_ID (oc_xxx)")
        r = call_app_api("POST", "/im/v1/messages?receive_id_type=chat_id",
                         {"receive_id": chat, "msg_type": "interactive",
                          "content": json.dumps(content, ensure_ascii=False)})
    if r.get("code") != 0:
        raise RuntimeError(f"send card failed: code={r.get('code')} msg={redact(r.get('msg', ''))}")
    mid = (r.get("data") or {}).get("message_id", "")
    return f"sent (message_id: {mid})" if mid else "sent"


def send_feishu_cardkit_detail(title: str, body: str, stats: dict | None = None,
                               status: str = "ok", chat_id: str = "",
                               card: dict | None = None,
                               reply_to_message_id: str = "") -> dict:
    """建终态卡 → 按引用发群，返回 {"card_id", "message_id", "detail"}。"""
    card_id = create_card(card or build_card(title, body, stats=stats, status=status))
    detail = send_card(card_id, chat_id, reply_to_message_id=reply_to_message_id)
    mid = ""
    m = re.search(r"message_id:\s*([^\s\)]+)", detail)
    if m:
        mid = m.group(1)
    return {"card_id": card_id, "message_id": mid, "detail": detail}


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
    return send_feishu_cardkit_detail(title, body, stats=stats, status=status,
                                      chat_id=chat_id, card=card)["detail"]


# ── 渠道三：dingtalk-webhook（钉钉自定义机器人，markdown 消息）────────────────

def render_dingtalk(title: str, body: str, stats: dict | None = None,
                    status: str = "ok", allow_summarize: bool = True) -> dict:
    """统一卡片模型 → 钉钉 markdown 消息载荷。

    钉钉自定义机器人没有交互卡/CardKit，markdown 就是全部版面：
    标题行 + 正文 + --- + 统计脚注一行（脚注仍只在完成态出现）。
    正文超长按 fit_body 处理（LLM 总结 → 硬截断兜底）。
    """
    if status not in STATUS_TEMPLATE:
        raise ValueError(f"unknown status: {status!r} (expect running/ok/error)")
    full_title = f"{STATUS_PREFIX[status]} {title.strip() or '通知'}"
    parts = [f"### {full_title}", "",
             fit_body(body.strip(), allow_summarize=allow_summarize) or "（空内容）"]
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
                         status: str = "ok", allow_summarize: bool = True) -> str:
    """统一卡片模型 → Telegram 纯文本（不启 parse_mode，避开 MarkdownV2 转义地雷）。

    标题、正文按原文、统计一行、「———」分隔；脚注仍只在完成态出现。
    正文超长按 fit_body 处理（LLM 总结 → 硬截断兜底），截的是正文，脚注保住。
    """
    if status not in STATUS_TEMPLATE:
        raise ValueError(f"unknown status: {status!r} (expect running/ok/error)")
    head = f"{STATUS_PREFIX[status]} {title.strip() or '通知'}\n\n"
    footer = stats_footer(stats)
    tail = f"\n\n———\n{footer}" if status != "running" and footer else ""
    budget = TELEGRAM_MAX_UNITS - _size(head + tail, "utf16")
    text = head + (fit_body(body.strip(), max(budget, 0), "utf16",
                            allow_summarize=allow_summarize) or "（空内容）") + tail
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
                token: str = "", dry_run: bool = False, secret: str = "",
                *,
                host: str = "", session_id: str = "", cwd: str = "",
                detail_body: str = "", tool_summary: str = "",
                context_style: str = "",
                panel_host_prefix: bool | None = None) -> tuple[bool, str]:
    """统一发送入口，fail-open：任何异常只落日志，返回 (是否成功, 打码后的说明)。

    dry_run=True 只返回将发送的载荷 JSON 字符串，不发网络请求。
    webhook/token/chat_id/secret 为路由目标级别的显式覆盖，缺省读各渠道环境变量
    （secret 是 webhook 的签名密钥，只对 feishu-webhook / dingtalk-webhook 有效）。
    context_style 显式覆盖面板头上下文样式（text / bar / text_bar，Studio 预览用），空 = 读设置。
    panel_host_prefix 显式覆盖面板头是否带宿主前缀（None = 读设置，默认关）。
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
        # dry-run/预览在 hook 前台同步执行：绝不在这里发起 LLM 总结的网络调用
        allow_sum = not dry_run
        # 规范化汇报载荷（title/body/stats/status）按渠道各自渲染
        if channel == "dingtalk-webhook":
            payload = render_dingtalk(title, body, eff_stats, status,
                                      allow_summarize=allow_sum)
            if dry_run:
                return True, json.dumps({"channel": channel, "lifecycle": "one-shot",
                                         "signed": bool(dingtalk_secret(webhook, secret)),
                                         "payload": payload}, ensure_ascii=False, indent=2)
            return True, send_dingtalk_webhook(payload, webhook, secret)
        if channel == "telegram":
            payload = {"chat_id": chat_id or _env("TELEGRAM_CHAT_ID") or "<TELEGRAM_CHAT_ID>",
                       "text": render_telegram_text(title, body, eff_stats, status,
                                                    allow_summarize=allow_sum)}
            if dry_run:
                return True, json.dumps({"channel": channel, "lifecycle": "one-shot",
                                         "payload": payload}, ensure_ascii=False, indent=2)
            return True, send_telegram(title, body, stats=stats, status=status,
                                       chat_id=chat_id, token=token)

        # feishu 两渠道（含 "-" 无渠道 dry-run 预览）：Card 2.0 终态卡
        logo_key = ""
        if channel == "feishu-cardkit" and not dry_run and eff_stats:
            logo_key = resolve_vendor_logo_image_key(str(eff_stats.get("model") or ""), host=host)

        use_interactive = (channel == "feishu-cardkit" and status != "running"
                           and interactive_enabled() and bool(host or session_id))
        ctx_id = ""
        resumable = False
        sess_mod = None
        if use_interactive:
            try:
                import feige_interactive as sess_mod  # noqa: E402
                if dry_run:
                    ctx_id = "preview"
                    resumable = sess_mod.is_host_resumable(host)
                else:
                    resumable = sess_mod.is_host_resumable(host, session_id=session_id)
                    eff_chat = chat_id or (project_chat_id(str((eff_stats or {}).get("project") or ""))
                                           if (eff_stats or {}).get("project") else default_chat_id())
                    ctx_id = sess_mod.register_session(
                        host=host,
                        session_id=session_id,
                        cwd=cwd,
                        project=str((eff_stats or {}).get("project") or ""),
                        model=str((eff_stats or {}).get("model") or ""),
                        title=title,
                        chat_id=eff_chat,
                        last_body=body,
                        detail_body=detail_body,
                        tool_summary=tool_summary,
                    )
            except Exception as exc:
                log(f"interactive context skipped (fail-open): {redact(exc)}")
                ctx_id = ""
                use_interactive = False

        card = build_card(
            title, body, stats=eff_stats, status=status,
            allow_summarize=allow_sum,
            detail_body=detail_body,
            tool_summary=tool_summary,
            host=host,
            ctx_id=ctx_id,
            interactive=use_interactive,
            resumable=resumable,
            logo_image_key=logo_key,
            context_style=context_style,
            panel_host_prefix=panel_host_prefix,
        )
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
        sent = send_feishu_cardkit_detail(title, body, chat_id=chat_id, card=card)
        if use_interactive and ctx_id and sess_mod is not None:
            try:
                sess_mod.update_session(
                    ctx_id,
                    card_id=sent.get("card_id", ""),
                    message_id=sent.get("message_id", ""),
                    chat_id=chat_id or default_chat_id(),
                )
            except Exception:
                pass
        return True, sent["detail"]
    except Exception as exc:  # fail-open：发卡失败只落日志，绝不抛炸调用方
        detail = redact(exc)
        log(f"send failed ({channel}): {detail}")
        return False, detail


# ── 群路由（项目 → 多目标 fanout）───────────────────────────────────────────

def routes_file_path(env: dict | None = None) -> str:
    """路由文件路径：FEIGE_ROUTES_FILE > 设置文件 routes_file > ~/.feige-routes.json。

    显式传 env（测试注入）时只看该字典，不掺设置文件。
    """
    if env is not None:
        override = str(env.get("FEIGE_ROUTES_FILE") or "").strip()
    else:
        override = _env("FEIGE_ROUTES_FILE")
    return override or str(Path.home() / ".feige-routes.json")


def settings_file_path(env: dict | None = None) -> str:
    """设置文件路径：FEIGE_SETTINGS_FILE > ~/.feige-settings.json。"""
    env = env if env is not None else os.environ
    override = str(env.get("FEIGE_SETTINGS_FILE") or "").strip()
    if override:
        return override
    return str(Path.home() / ".feige-settings.json")


def load_settings(path: str = "") -> dict:
    """读设置文件；不存在/损坏返回 {}（fail-open，未配置是常态）。

    不缓存：Studio 保存后后台发送进程要立刻读到新值（发送路径每键一查，
    小 JSON 文件读取成本可忽略，缓存的 mtime 粒度反而可能在 Windows 上读到旧值）。
    """
    try:
        doc = json.loads(Path(path or settings_file_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


_STATS_SCOPES = ("turn", "session")


def stats_scope(env: dict | None = None, settings: dict | None = None) -> str:
    """统计口径：turn（单轮，默认）| session（全程）。

    优先级：FEIGE_STATS_SCOPE（插件设置经 common.apply_plugin_options 也落到这里）
    > ~/.feige-settings.json 的 stats_scope > turn。
    """
    env = env if env is not None else os.environ
    v = str(env.get("FEIGE_STATS_SCOPE") or "").strip().lower()
    if v in _STATS_SCOPES:
        return v
    v = str((load_settings() if settings is None else settings).get("stats_scope") or "").strip().lower()
    return v if v in _STATS_SCOPES else "turn"


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
                doc: dict | None = None,
                *,
                host: str = "", session_id: str = "", cwd: str = "",
                detail_body: str = "", tool_summary: str = "") -> tuple[bool, str]:
    """按路由 fanout：每个目标独立发送（单目标失败不影响其余），返回聚合结果。

    无路由文件 / 文件损坏 / 项目无匹配且无 "*" 兜底：退化为现有单渠道行为，
    但目标群仍按项目解析（project_chat_id）——「只配项目群、不配路由」是最轻量的
    每群开关用法，不能因为没写路由文件就发进全局默认群。
    """
    doc = load_routes() if doc is None else doc
    targets = resolve_routes(project, doc) if doc else []
    extra = {k: v for k, v in (
        ("host", host), ("session_id", session_id), ("cwd", cwd),
        ("detail_body", detail_body), ("tool_summary", tool_summary),
    ) if v}
    if not targets:
        if doc is not None:
            log(f"no route for project {project or '(none)'!r} (no \"*\" fallback), single-channel")
        return send_report(title, body, stats=stats, status=status, dry_run=dry_run,
                           chat_id=project_chat_id(project) if project else "", **extra)
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
            # 目标没写 chat_id → 落到「本项目对应的群」（没配项目键则是全局默认群），
            # 而不是空串：空串会被 send_report 当成「用默认」，但默认群恰恰是
            # 用户想按项目分开的那个值。
            chat_id=str(t.get("chat_id") or "") or (project_chat_id(project) if project else ""),
            webhook=str(t.get("webhook") or ""),
            token=str(t.get("token") or ""), secret=str(t.get("secret") or ""),
            dry_run=dry_run, **extra)
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


# 后台发送进程开卡前重读转录的等待秒数（0 关闭）。宿主写 jsonl 可能略滞后于
# Stop 触发，hook 前台不能等（零阻塞不变量），让 detached 发送进程等一下再重读。
READ_BODY_DELAY_SECONDS = 1.5


def read_body_delay_seconds() -> float:
    try:
        return max(float(_env("FEIGE_READ_BODY_DELAY") or READ_BODY_DELAY_SECONDS), 0.0)
    except ValueError:
        return READ_BODY_DELAY_SECONDS


def run_send(args: argparse.Namespace) -> int:
    """执行一次 send（供 main 与适配器后台入口共用）。

    带 --transcript 时先等 FEIGE_READ_BODY_DELAY（默认 1.5s）再重读转录取最新
    收尾正文——治宿主落盘滞后导致的「战报内容是上一轮」；重读失败（文件被
    轮转/解析不出）保持 hook 前台已提取的正文，照发不误（fail-open）。
    """
    wait = args.debounce if args.debounce is not None else debounce_seconds()
    if args.debounce_key and wait > 0 and not args.dry_run:
        if not debounce_wait(args.debounce_key, wait):
            log(f"superseded by a newer stop within {wait:g}s, skipped")
            return 0
    if getattr(args, "transcript", "") and not args.dry_run:
        fresh = refresh_body_from_transcript(args)
        if fresh:
            log(f"body refreshed from transcript ({fresh} chars)")
    stats = {
        "project": args.project, "model": args.model,
        "thinking": args.thinking, "tools": args.tools,
        "context": args.context, "tokens": args.tokens, "elapsed": args.elapsed,
    }
    extra = {
        "host": getattr(args, "host", "") or "",
        "session_id": getattr(args, "session_id", "") or "",
        "cwd": getattr(args, "cwd", "") or "",
        "detail_body": getattr(args, "detail", "") or "",
        "tool_summary": getattr(args, "tool_summary", "") or "",
    }
    if args.route:
        ok, detail = send_routed(args.title, args.body, stats=stats,
                                 status=args.status, project=args.project,
                                 dry_run=args.dry_run, **extra)
    else:
        ok, detail = send_report(args.title, args.body, stats=stats, status=args.status,
                                 channel=args.channel, chat_id=args.chat_id,
                                 webhook=args.webhook, dry_run=args.dry_run, **extra)
    print(detail if args.dry_run else (f"✅ {detail}" if ok else f"❌ {detail}"))
    return 0 if ok else 1


_EMPTY_NOTE = "……（本轮无文本输出）"  # 与 adapters/common.body_for_card 占位符一致


def refresh_body_from_transcript(args: argparse.Namespace) -> int:
    """后台进程重读转录，把最新收尾正文/统计换进 args（治落盘滞后）。

    宿主在 Stop 触发瞬间可能还没把本轮最终文本写进 jsonl；hook 前台不能等
    （零阻塞不变量），后台发送进程可以。做法：等 read_body_delay_seconds()
    → 用 Claude Code 适配器（转录 jsonl 口径）重解析 --transcript 指向的文件。
    规则：
    - 重读出的正文是占位符（新一轮已开始但文本还没落盘）→ 不替换，保持 hook 正文；
    - 重读正文与 hook 不同 → 替换，并同步刷新模型/思考/工具/token/耗时/上下文
      （同一轮口径，晚读到的那份才是收尾后的完整统计）；
    - 文件不存在/解析失败 → 保持 hook 正文照发（fail-open）。
    返回新正文长度（0 = 未刷新）。
    """
    transcript = str(getattr(args, "transcript", "") or "").strip()
    if not transcript or not Path(transcript).is_file():
        return 0
    delay = read_body_delay_seconds()
    if delay > 0:
        time.sleep(delay)
    try:
        import importlib.util
        root = Path(__file__).resolve().parent / "adapters"
        if Path(transcript).name == "wire.jsonl":  # kimi-code 会话
            mod_path, fn = root / "kimi-code" / "stop_notify.py", "summarize_wire"
        elif "antigravity" in Path(transcript).as_posix():  # agy 会话（brain/**/transcript_full.jsonl）
            mod_path, fn = root / "antigravity" / "stop_notify.py", "summarize_transcript"
        else:  # Claude Code / 其余按转录 jsonl 口径
            mod_path, fn = root / "claude-code" / "stop_notify.py", "summarize_transcript"
        mod_spec = importlib.util.spec_from_file_location("feige_refresh_" + fn, mod_path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)  # 目录名带连字符，按路径加载（同 hooks/stop.py）
        summary = getattr(mod, fn)(transcript)
        if not summary:
            return 0
        # kimi 口径里上下文是 ctx/raw_model，在 main() 才格式化——这里补齐同口径
        if "context" not in summary and summary.get("ctx") is not None:
            summary["context"] = mod.fmt_context(
                summary["ctx"], mod.context_window(summary.get("raw_model") or ""))
        new_body = str(summary.get("body") or "").strip()
        if new_body and new_body != _EMPTY_NOTE and new_body != args.body:
            args.body = new_body
            for key in ("model", "thinking", "tools", "context", "tokens", "elapsed", "detail", "tool_summary"):
                if summary.get(key) is not None:
                    setattr(args, key, summary[key])
            return len(new_body)
    except Exception as exc:  # fail-open：刷新失败照发 hook 提取的正文
        log(f"transcript refresh failed (sending hook body): {redact(exc)}")
    return 0


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
    send.add_argument("--thinking", type=int, default=None, help="思考轮数（💭）")
    send.add_argument("--tools", type=int, default=None, help="工具调用数（🔧）")
    send.add_argument("--context", default="", help="上下文水位，如 42%% 或 86.5k/200.0k (43%%)")
    send.add_argument("--tokens", type=int, default=None, help="累计输出 token 数（🎫）")
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
    send.add_argument("--transcript", default="",
                      help="宿主转录 jsonl 路径；传了则后台进程稍等后重读它取最新收尾正文"
                           "（治宿主落盘滞后导致的战报落后一轮），重读失败保持 --body")
    send.add_argument("--host", default="", help="宿主标识（claude-code / codex / zcode / kimi-code / antigravity）")
    send.add_argument("--session-id", default="", help="宿主原生会话 ID（仅存本地安全映射，不上卡）")
    send.add_argument("--cwd", default="", help="会话工作目录（仅存本地安全映射，不上卡）")
    send.add_argument("--detail", default="", help="折叠面板承载的详细正文/原文摘录")
    send.add_argument("--tool-summary", default="", help="折叠面板承载的工具调用摘要")

    sub.add_parser("listen", help="前台运行飞书长连接（WebSocket）交互回调监听")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:  # Windows 终端 emoji/中文输出兜底
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.command == "send":
        return run_send(args)
    if args.command == "listen":
        import feige_interactive
        return feige_interactive.run_ws_listener_forever()
    return 2


if __name__ == "__main__":
    sys.exit(main())
