---
name: feige
description: 用飞鸽（feige-fry-cards）把任务结果以摘要战报卡直发飞书群。Use when the user asks to 发战报/结果卡片到飞书群、汇报任务结果到群里、会话结束自动通知飞书, send a battle-report/summary card to a Feishu group, or asks about feige / 📦·💭·🔧·⏱️ style status cards. Runs the local `feige.py` CLI (webhook 或 CardKit 双渠道自动选择).
---

# feige — 摘要战报卡直发飞书群

`feige.py`（本插件根目录，纯 Python 标准库）是 agent 无关的结果汇报核心：
**标题 + ≤300 字摘要正文 + 统计脚注 + 状态着色**，一张卡片直发飞书群。

## 发送命令

```bash
python <plugin root>/feige.py send \
    --title "任务完成" \
    --body "**一句话战报**：改了什么、结果如何" \
    --status ok \
    [--project 项目名] [--model 模型] [--thinking N] [--tools N] \
    [--context "42%"] [--elapsed "2m38s"] \
    [--channel feishu-webhook|feishu-cardkit] [--chat-id oc_xxx] [--dry-run]
```

机器上 `python` 不在 PATH 时用 `py -3` 代替。Windows 终端中文乱码时前缀
`PYTHONIOENCODING=utf-8`。

## 铁律

- **摘要是战报不是镜像**：`--body` 是给群里看的结果汇报（做了什么 → 什么结果 → 关键数字），
  不是把完整回复/长日志贴进去；超过 300 字级的内容先自己提炼再发。
- **统计字段**能带就带：`--project`（📦）、`--model`、`--thinking`（💭 思考轮数）、
  `--tools`（🔧 工具调用数）、`--context`（上下文水位，形如 `42%` 或 `86.5k/200.0k (43%)`）、
  `--elapsed`（⏱️ 耗时）。它们会拼成一行 notation 脚注，只在 ok/error 态出现。
- **状态着色**：成功 `--status ok`（绿）、失败/中断 `--status error`（红，正文写清错误
  与下一步）、进行中 `--status running`（蓝）。
- 不知道统计数字就别编——空字段会自动省略，但不许虚构。

## 渠道与凭据

渠道自动选择：有 `FEISHU_CARD_WEBHOOK` / `FEISHU_WEBHOOK_URL` 走自定义机器人
webhook（一次性整卡）；否则有 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 走 CardKit
（建卡 → 按引用发群，目标群 `FEISHU_NOTIFY_CHAT_ID` 或 `--chat-id`）。
机器人开了签名校验就配 `FEISHU_WEBHOOK_SECRET`。正文超长会按渠道上限自动截断，
但仍应自己先提炼。
也可在插件设置里填（ZCode 注入为 ZCODE_USER_CONFIG_*，Claude Code 注入为
CLAUDE_PLUGIN_OPTION_*，Stop hook 会自动映射；手动调 CLI 时仍以环境变量为准）。

发送失败时：读 stderr 里打码后的错误，告诉用户该配哪个变量（webhook URL /
app 凭据 / chat id），不要猜 chat id、不要重试轰炸。`--dry-run` 先预览卡片 JSON
再真发。

## Stop hook 自动战报

插件带 Stop hook（默认关，Claude Code / Codex / ZCode 通用）：`FEIGE_HOOK_NOTIFY=1` 或插件
设置打开 hook_notify 后，每轮收尾自动提取本会话摘要与统计发卡——日常靠它即可，
本技能用于“现在主动发一张”的场景；hook 已开时别再为同一结果重复发卡。
