# 🕊️ feige-fry-cards — 飞鸽卡片

> 飞鸽传书——把 agent 的战报投进任何群。fry-cards 系列第 N 只。

一个 **agent 侧的结果汇报插件**：任何编码 agent（ZCode、Claude Code、Codex……）的会话
结束时，自动生成**摘要结果卡片**，直发飞书群。不依赖任何 agent 产品的内置渠道，
卡片形态完全自控。

## 系列对齐（fry-cards 家族）

| 仓库 | emoji | 宿主 agent | 形态 |
|------|-------|-----------|------|
| [hermes-fry-cards](https://github.com/techysy/hermes-fry-cards) | 🍟 薯条 | Hermes Gateway | 通道内流式卡片插件（系列源头） |
| [claw-fry-cards](https://github.com/techysy/claw-fry-cards) | 🍤 虾条 | OpenClaw | 飞书通道插件（替代官方通道） |
| [zcode-feishu-bridge](https://github.com/techysy/zcode-feishu-bridge) | 🌉 | ZCode | 日志 tail 桥接守护进程（流式卡片） |
| mimo-fry-cards（本地探针） | — | MiMo Desktop / MiMoCode | 飞书 fry 风格通道（探针阶段） |
| zcode-feishu-card（本地） | — | ZCode | MCP 主动推卡插件（非流式，单次卡片） |
| **feige-fry-cards**（本仓） | 🕊️ 飞鸽 | **agent 无关** | **结果汇报插件**：会话收尾摘要卡直发群 |

命名规则沿用家族惯例 `{宿主}-{风格}-cards`；本仓宿主是飞鸽这个"信使"而非某个具体
agent，因为它面向的是**所有 agent**。

与兄弟仓库的分工：hermes / claw / mimo 那几只做的是**双向通道**（接管 IM 收发，渲染每一轮
流式回复）；zcode-feishu-bridge 是 ZCode 专属的日志桥接；zcode-feishu-card 是 ZCode 专属
的主动推卡。飞鸽做的是**跨 agent 的"结果汇报"这一件事**——把 zcode-feishu-card 的
webhook/skill 形态 + zcode-feishu-bridge 的 CardKit 流式卡核心抽出来，做成任何 agent
都能挂的通用汇报层。

## 立项背景

对 Mirasim 的评估（2026-09）证实了两件事：

1. "agent 进展汇报到 IM 群"是被验证的标准需求——Mirasim 把它做成了内置的 7 渠道双向遥控；
2. 但内置渠道**不给你卡片形态和注入的控制权**（闭源固定适配器、无渠道插件 SDK），
   想要"自定义摘要卡片进群"，正确姿势是在 agent 侧挂 skill，走各平台 webhook 直发。

飞鸽就是第二条路，也是 fry-cards 家族风格（流式卡片 / 统一面板 / 封卡统计）向
"跨 agent 汇报"场景的延伸。

> **实测勘误（2026-09-26）**：Mirasim 飞书官渠实机验证为**交互卡片**——会话状态条
> （`agent · 模型 · ⏱️ 耗时` 实时更新）+ Stop 按钮（卡片交互回调），群内 @bot 双向遥控
> 完整可用；此前"大概率富文本、无卡片承诺"的推测作废。结论修正为：Mirasim 托管的
> 会话由官渠良好覆盖，feige 不与其竞争；feige 的领地是 **Mirasim 托管体系之外的
> 裸跑 agent 进程**（本机直起的 ZCode / Claude Code / Codex CLI），以及卡片风格的
> 完全自控（fry 美学、统计面板口径）。

## 设计原则（继承自 bridge / 家族的踩坑）

- **摘要是战报，不是镜像**：卡片正文截断（300 字级），全文永远留在 agent 客户端/官方通道。
- **统计行只在封卡出现**：进行中的卡不堆指标；综合面板（模型 · 💭 · 🔧 · 上下文 · ⏱️）
  是收尾脚注，单次呈现——与 hermes/claw 的统一面板口径对齐。
- **fail-open**：发卡失败绝不能拖垮 agent 会话；异常只落日志。
- **密钥只走环境变量**：`FEISHU_APP_ID/SECRET`、webhook URL 一律不入库、不进日志。
- **Python 标准库 only**（CLI/核心），单文件核心优先；文档与注释用中文。

## 架构

```
┌───────────── 入口（可插拔） ─────────────┐
│ skill 调用   hook 回调   CLI   tail 守护  │  ← 各 agent 一层薄适配
└──────────────────┬────────────────────────┘
                   ▼
        feige 核心（单文件 feige.py）
        会话摘要 → 卡片拼装 → sequence/重试/限流
                   ▼
┌──────────── 渠道（可插拔） ──────────────┐
│ 飞书 webhook ✓  飞书 CardKit  钉钉  TG … │  ← 各渠道一个 adapter，群路由走配置
└───────────────────────────────────────────┘
```

- **入口**决定"什么时候汇报"：agent 会话结束的 Stop hook、`feige send` 手动调用、
  或保留 tail 模式兼容 ZCode 现状。
- **渠道**决定"发到哪、什么形态"：每个渠道一个薄 adapter，输入统一的卡片模型
  （标题 / 摘要正文 / 统计脚注 / 状态），输出各平台消息。
- **群路由**：一份配置把「项目 → 群 webhook」映射起来，支持一个项目多群。

## 快速开始

```bash
# 1. 手动发一张战报卡（最快 30 秒见效；凭据见「渠道矩阵」）
export FEISHU_APP_ID=... FEISHU_APP_SECRET=... FEISHU_NOTIFY_CHAT_ID=oc_...
python feige.py send --title "hello" --body "**feige 上线**" \
       --project demo --model kimi-k3 --elapsed 0m30s --dry-run   # 先看载荷,去掉 --dry-run 真发

# 2. 接入你的 agent，收尾自动发
#    ZCode / Claude Code / Codex → 见「安装与启用」「各 agent 安装」

# 3. 打开设置页（渠道状态、群路由编辑、卡片预览、接入自检）
python webui.py            # → http://127.0.0.1:8787
```

完整说明：[CLI 用法](#cli-用法) · [渠道矩阵](#渠道矩阵) · [群路由](#群路由项目--多目标-fanout) ·
[安装与启用（ZCode）](#安装与启用zcode-插件) · [各 agent 安装](#各-agent-安装claude-code--codex) ·
[WebUI 设置页](#webui-设置页) · [联调排错](#联调排错)

## 里程碑

- **M1 发卡核心** ✅（2026-09-26 完成）：从 zcode-feishu-bridge 抽 CardKit 流式卡生命
  周期，从 zcode-feishu-card 抽 webhook 双通道发送，合成独立单文件库 `feige.py`，提供
  `feige send --title … --body … [--channel feishu-webhook]` CLI。
- **M2 ZCode 接入** ✅（2026-09-26 完成）：以 skill/hook 形式接入 ZCode，会话收尾自动
  发摘要战报卡（Stop hook，默认关）；`.zcode-plugin/` 清单、`hooks/`、`skills/feige/`
  就位，与现有 bridge 守护进程并存切换（见下文「安装与启用」）。
- **M3 Claude Code / Codex 接入** ✅（2026-09-26 完成）：`adapters/claude-code/stop_notify.py`
  （Stop hook + transcript 统计）与 `adapters/codex/notify.py`（notify 事件 → teaser 卡），
  直接 import feige.py 调 `send_report()`，验证"agent 无关"成立（见「各 agent 安装」）。
- **M4 多渠道 + 群路由** ✅（2026-09-26 完成）：新增 `dingtalk-webhook`（钉钉 markdown
  消息）与 `telegram`（Bot API 纯文本）两个渠道，渠道注册表扩至 4 个；`--route` 按
  路由文件做项目 → 多目标 fanout（见「渠道矩阵」「群路由」）。

- **M5 独立设置 WebUI** ✅（2026-09-26 完成）：`webui.py`（纯标准库 `http.server`，
  只绑 127.0.0.1），总览/群路由编辑/卡片预览/测试发送/接入自检五页（见「WebUI 设置页」）。

**当前状态**：五个里程碑全部完成。feige 已从 ZCode 专属脚本演进为可用的跨 agent
多渠道汇报核心 + 本地设置界面；后续方向：各渠道的群路由实测联通、tail 守护模式
（`feige.py` 已留好 CardKit 生命周期 import 口）。

## CLI 用法

```bash
python feige.py send --title 标题 --body "markdown 正文" \
    [--project X] [--model X] [--thinking N] [--tools N] \
    [--context 42%] [--elapsed 2m38s] \
    [--status ok|error|running] \
    [--channel feishu-webhook|feishu-cardkit|dingtalk-webhook|telegram] \
    [--chat-id ...] [--webhook ...] [--route] [--dry-run]
```

- **状态着色**（与家族一致）：`running` 蓝、`ok` 绿（默认）、`error` 红；
- **统计脚注**只在 `ok`/`error` 态单次呈现：`📦 项目 · 模型 · 💭思考 · 🔧工具 · 上下文 · ⏱️ 耗时`，
  空字段自动省略；
- **渠道默认选择**：有飞书 webhook 环境变量走 `feishu-webhook`（一次性整卡，无流式），
  否则有应用凭据走 `feishu-cardkit`（建卡 → 正文更新 → 封卡 → 按引用发群）；
  钉钉/Telegram 不进默认选择，需显式 `--channel` 或 `--route` 路由；都没有则明确报错（打码后）；
- **`--webhook`**：渠道级 webhook 覆盖（feishu-webhook / dingtalk-webhook）；
- **`--route`**：按 `--project` 解析路由文件做多目标 fanout（见「群路由」），不带则保持单渠道；
- **`--dry-run`** 只打印将发送的载荷 JSON，不发网络请求、不要求凭据；
- **fail-open**：发卡失败只落日志/返回非零退出码，绝不抛炸调用方。

环境变量：

| 变量 | 用途 |
|------|------|
| `FEISHU_CARD_WEBHOOK` / `FEISHU_WEBHOOK_URL` | 飞书自定义机器人 webhook |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭据（CardKit 通道） |
| `FEISHU_BASE_URL` | 默认 `https://open.feishu.cn` |
| `FEISHU_NOTIFY_CHAT_ID` | 飞书默认群 `oc_xxx`（CardKit 通道必配） |
| `DINGTALK_WEBHOOK` | 钉钉自定义机器人 webhook |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram bot token / 目标 chat（群为负数 id） |
| `FEIGE_ROUTES_FILE` | 群路由文件（默认 `~/.feige-routes.json`） |

库用法（后续 tail 模式 / 各 agent 入口 import 复用）：

```python
import feige
feige.send_report("会话收尾", "完成 3 个文件修改",
                  stats={"project": "feige-fry-cards", "model": "glm-5",
                         "thinking": 4, "tools": 12, "context": "42%", "elapsed": "2m38s"},
                  status="ok")  # -> (True, "sent (webhook)")
# CardKit 流式卡生命周期也可单独取用：
# feige.create_card / update_content / seal_card / send_card / build_card
```

## 渠道矩阵

| 渠道 | 凭据环境变量 | 版面能力 | 说明 |
|------|-------------|---------|------|
| `feishu-webhook` | `FEISHU_CARD_WEBHOOK` / `FEISHU_WEBHOOK_URL` | 交互卡（Card 2.0） | 一次性整卡，最简单 |
| `feishu-cardkit` | `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（+ `FEISHU_NOTIFY_CHAT_ID`） | 交互卡 + 流式 | 建卡 → 封卡 → 引用发群，生命周期函数可被 tail 模式复用 |
| `dingtalk-webhook` | `DINGTALK_WEBHOOK`（或 `--webhook`） | markdown 消息 | 标题行 + 正文 + `---` + 统计脚注；无交互卡概念 |
| `telegram` | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`（chat 可 `--chat-id`） | 纯文本 | 不启 parse_mode，避开 MarkdownV2 转义地雷 |

统一卡片模型（标题/正文/统计/状态）为输入，各渠道各自渲染；统计脚注只在完成态
（ok/error）出现的口径跨渠道一致。

**钉钉最低配置**：群 → 群设置 → 智能群助手 → 添加机器人 → 自定义，复制 webhook
（安全设置选"自定义关键词"时，正文里带上关键词，例如固定标题词）。
**Telegram 最低配置**：找 @BotFather 领 token；把 bot 拉进目标群后，用
`https://api.telegram.org/bot<token>/getUpdates` 查看 `chat.id`（群为负数，如 `-100…`）。

## 群路由（项目 → 多目标 fanout）

路由文件为 JSON，路径取 `FEIGE_ROUTES_FILE`，默认 `~/.feige-routes.json`（**不进仓库**；
可含 webhook/chat_id 等半敏感信息，注意文件权限，秘密建议用 `$ENV` 引用留在环境变量里）：

```json
{
  "routes": {
    "feige-fry-cards": [
      {"channel": "feishu-cardkit"},
      {"channel": "dingtalk-webhook", "webhook": "$DINGTALK_WEBHOOK"}
    ],
    "*": [ {"channel": "feishu-cardkit"} ]
  }
}
```

- **匹配**：精确项目名 → `"*"` 通配兜底 → 无匹配时退化为现有单渠道行为（不打断
  M2/M3 调用方）；
- **目标字段**：`channel` 必填；`webhook` / `chat_id` / `token` 为该目标的显式覆盖，
  缺省读各渠道环境变量；整字符串值 `"$NAME"` 引用为环境变量值（秘密不进路由文件）；
- **fanout**：逐目标独立发送，单目标失败不影响其余，返回聚合结果；目标缺/坏
  `channel` 会跳过并落日志（打码）。

用法（dry-run 是先验证路由的正确姿势）：

```bash
export FEIGE_ROUTES_FILE=/path/to/routes.json DINGTALK_WEBHOOK=……
python feige.py send --title 战报 --body "完成 X" --project feige-fry-cards --route --dry-run
# 解析出 N 个目标就打印 N 份载荷，确认无误后去掉 --dry-run 真发
```

## WebUI 设置页

```bash
python webui.py [--port 8787]   # 打开 http://127.0.0.1:8787
```

对标 hermes-fry-cards 的 studio 工作坊，但走本项目的纯 Python 标准库路线
（`http.server` 手写，零第三方依赖）。五个页面：

| 页面 | 功能 |
|------|------|
| `GET /` 总览 | 4 渠道凭据状态（只显"已配置/未配置"）、路由文件状态；附各渠道测试发送按钮（未配置禁用） |
| `GET+POST /routes` | 群路由表单化编辑：缺 channel 拒绝、保存前 `.bak` 备份 + 原子写、`$ENV` 引用原样保留、保存后按 `resolve_routes` 语义冒烟 |
| `GET /preview` | 填参数出两部分：feige dry-run 载荷 JSON（原样）+ 朴素 HTML 外观示意 |
| `POST /test` | 发送固定内容测试卡「🕊️ feige WebUI 测试卡」，结果打码后展示 |
| `GET /adapters` | 接入自检（只读）：ZCode 插件注册 / Claude settings.json 片段 / Codex notify 行（含冲突提示），未接入项给 README 指引 |

**安全说明**：只监听 `127.0.0.1`，**无鉴权**——局域网/公网都到不了，但不要改绑定地址、
不要把端口转发出去。密钥硬规则与 `feige.redact` 同级：任何响应体绝不回显
`FEISHU_APP_SECRET` / bot token / webhook key 的值（凭据列只有"已配置/未配置"）。
所有处理函数 fail-open：异常显示为页面内错误条而非 500 白屏。

## 命名

**飞鸽 Feige**：飞鸽传书，谐音飞书之"飞"；CLI 命令 `feige`。
仓库名按家族惯例对齐为 `feige-fry-cards`。

## 安装与启用（ZCode 插件）

仓库即插件，目录结构对齐家族规范：

```
feige-fry-cards/
├── .zcode-plugin/{plugin,marketplace}.json   # 插件清单（userConfig: 凭据/hook 开关等）
├── hooks/{hooks.json,stop-notify.mjs}        # Stop hook：收尾自动发战报卡
├── skills/feige/SKILL.md                     # 让模型主动调 feige send
└── feige.py                                  # 核心 + CLI（M1）
```

1. **发现**：在 ZCode 插件市场里添加本目录（`.zcode-plugin/marketplace.json` 指向 `.`），
   安装 feige-fry-cards 插件。
2. **凭据**：插件 Settings 里填 webhook_url（推荐，最简单）或 app_id/app_secret +
   notify_chat_id；也可只配系统环境变量（`FEISHU_CARD_WEBHOOK` 等），真实环境变量优先于
   Settings。
3. **打开 Stop hook 战报**（默认关）：插件 Settings 打开 `hook_notify`，或设环境变量
   `FEIGE_HOOK_NOTIFY=1`。之后每个任务收尾自动发卡：hook 从
   `~/.zcode/cli/rollout/model-io-sess_*.jsonl` 定位本会话（payload.session_id 命中文件名，
   否则取 mtime 最新兜底），解析最后一条 `finishReason=="stop"` 行做 ≤300 字摘要，
   统计全会话的 💭/🔧/上下文水位/⏱️，调用 `feige.py send --status ok …`（detached +
   unref，绝不阻塞 ZCode；任何异常 exit 0）。
4. **调试**：`FEIGE_DRY_RUN=1` 时 hook 改为同步执行并把 feige 命令与卡片 JSON 打到
   stderr，不发网络；测试可用 `ROLLOUT_DIR=<目录>` 覆盖日志目录。

### 与 zcode-feishu-bridge 的关系

两者**定位不同、不冲突**：bridge 守护进程是**过程流式镜像**（每一轮打字机直播到专属群），
feige Stop hook 是**收尾战报**（任务结束一张摘要卡）。可以并存（直播看过程 + 战报进
大群），也可以只开其一。注意若同时开，**同一个群会收到两张口径不同的卡**，建议 bridge
走专属小群、feige 走汇报群。

### hook 环境变量的坑（联调实录）

hook 在 **ZCode 进程内环境**运行，拿到的是 ZCode **启动时**的环境快照：
`setx` / 改注册表对已在运行的 ZCode 无效（同 M1 联调的 230002 排错）。
在插件 Settings 里填凭据更可靠——hook 会把 `ZCODE_USER_CONFIG_*` 注入 feige 子进程
（真实环境变量优先）。验证配置时请显式传最新值。

## 各 agent 安装（Claude Code / Codex）

适配器在 `adapters/`，纯 Python、直接 import feige.py（不走 subprocess）。**全部默认关闭**：
`FEIGE_HOOK_NOTIFY=1` 才启用；`FEIGE_DRY_RUN=1` 时把卡片 JSON 打到 stdout 不发网络。
提取口径与 ZCode hook 一致（≤300 字 teaser + 统计脚注，拿不到的字段自动省略，绝不硬编）。

### Claude Code（Stop hook）

贴到 `~/.claude/settings.json`（自行合并到已有内容，路径按实际仓库位置改）：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python F:/Files/GitHub Files/feige-fry-cards/adapters/claude-code/stop_notify.py"
          }
        ]
      }
    ]
  }
}
```

hook 从 payload 的 `transcript_path` 解析转录：assistant 行按 `content[].type` 取
text（teaser）/ thinking（💭）/ tool_use（🔧），模型取 `message.model`，上下文水位 =
最后一条 assistant 的 `input_tokens + cache_read_input_tokens`（只报绝对值，不猜窗口），
⏱️ 取首末 timestamp；`isSidechain` 子代理行跳过。schema 已拿本机真实转录验证。

### Codex CLI（notify）

`~/.codex/config.toml` 顶层加/改：

```toml
notify = ["python", "F:/Files/GitHub Files/feige-fry-cards/adapters/codex/notify.py"]
```

事件 JSON 经**最后一个 argv** 传入（本机 codex.exe 二进制实证字段：`type` /
`thread-id` / `turn-id` / `cwd` / `client` / `input-messages` /
`last-assistant-message`）。只在 `type=="agent-turn-complete"` 时发卡；Codex notify 不给
token/工具统计，卡片只带 teaser + 项目 + 模型（config.toml 顶层 `model = "..."`，
读不到就省略）。

**注意**：Codex 只有**一条** notify 命令——已有 `notify = [...]`（如 computer-use）的
用户需自行写一个包装脚本同时调两边。

### 陈旧环境变量提醒（三个 agent 同样适用）

hook/notify 都跑在 **agent 进程的环境快照**里：`setx` 或新改的系统环境变量对已在运行的
agent 无效。ZCode 插件走 userConfig 注入兜底；Claude Code / Codex 没有插件配置层——
改完凭据**重启 agent 会话**再验证，排错时先按「联调排错」节核对环境。

## 联调排错

- **`230002 Bot/User can NOT be out of the chat`**：先查机器人在不在群（`GET /im/v1/chats`
  列出 bot 所在群；不在就拉进群）。再查环境变量是不是陈旧的——**`setx` 不影响已运行
  进程**，守护进程/长会话终端里的 `FEISHU_APP_ID` 可能落后于注册表；用
  `reg query "HKCU\Environment" /v FEISHU_APP_ID` 对一下，验证时显式传最新值。
