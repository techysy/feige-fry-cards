# 🕊️ feige-fry-cards — 飞鸽卡片

> 飞鸽传书——把 agent 的战报投进任何群。fry-cards 系列第 N 只。

一个 **agent 侧的结果汇报插件**：任何编码 agent（ZCode、Claude Code、Codex……）的会话
结束时，自动生成**摘要结果卡片**，直发飞书群。不依赖任何 agent 产品的内置渠道，
卡片形态完全自控。

## 系列对齐（fry-cards 家族）

| 仓库 | emoji | 宿主 agent | 形态 |
|------|-------|-----------|------|
| [hermes-fry-cards](../hermes-fry-cards) | 🍟 薯条 | Hermes Gateway | 通道内流式卡片插件（系列源头） |
| [claw-fry-cards](../claw-fry-cards) | 🍤 虾条 | OpenClaw | 飞书通道插件（替代官方通道） |
| [mimo-fry-cards](../mimo-fry-cards) | — | MiMo Desktop / MiMoCode | 飞书 fry 风格通道（探针阶段） |
| [zcode-feishu-bridge](../zcode-feishu-bridge) | — | ZCode | 日志 tail 桥接守护进程（流式卡片） |
| [zcode-feishu-card](../zcode-feishu-card) | — | ZCode | MCP 主动推卡插件（非流式，单次卡片） |
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

## 里程碑

- **M1 发卡核心** ✅（2026-09-26 完成）：从 zcode-feishu-bridge 抽 CardKit 流式卡生命
  周期，从 zcode-feishu-card 抽 webhook 双通道发送，合成独立单文件库 `feige.py`，提供
  `feige send --title … --body … [--channel feishu-webhook]` CLI。
- **M2 ZCode 接入**：以 skill/hook 形式接入 ZCode，会话收尾自动发摘要卡，与现有
  bridge 守护进程并存切换。
- **M3 Claude Code / Codex 接入**：Claude Code 的 Stop hook、Codex 的 notify 机制，
  验证"agent 无关"成立。
- **M4 多渠道**：钉钉 webhook、Telegram Bot；群路由配置（项目 → 多群 fanout）。

## CLI 用法

```bash
python feige.py send --title 标题 --body "markdown 正文" \
    [--project X] [--model X] [--thinking N] [--tools N] \
    [--context 42%] [--elapsed 2m38s] \
    [--status ok|error|running] \
    [--channel feishu-webhook|feishu-cardkit] [--chat-id oc_xxx] [--dry-run]
```

- **状态着色**（与家族一致）：`running` 蓝、`ok` 绿（默认）、`error` 红；
- **统计脚注**只在 `ok`/`error` 态单次呈现：`📦 项目 · 模型 · 💭思考 · 🔧工具 · 上下文 · ⏱️ 耗时`，
  空字段自动省略；
- **渠道默认选择**：有 webhook 环境变量走 `feishu-webhook`（一次性整卡，无流式），
  否则有应用凭据走 `feishu-cardkit`（建卡 → 正文更新 → 封卡 → 按引用发群），
  都没有则明确报错（打码后）；
- **`--dry-run`** 只打印将发送的卡片 JSON，不发网络请求、不要求凭据；
- **fail-open**：发卡失败只落日志/返回非零退出码，绝不抛炸调用方。

环境变量（与 zcode-feishu-card 对齐）：

| 变量 | 用途 |
|------|------|
| `FEISHU_CARD_WEBHOOK` / `FEISHU_WEBHOOK_URL` | 自定义机器人 webhook（webhook 通道） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 应用凭据（CardKit 通道） |
| `FEISHU_BASE_URL` | 默认 `https://open.feishu.cn` |
| `FEISHU_NOTIFY_CHAT_ID` | 默认群 `oc_xxx`（CardKit 通道必配） |

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

## 命名

**飞鸽 Feige**：飞鸽传书，谐音飞书之"飞"；CLI 命令 `feige`。
仓库名按家族惯例对齐为 `feige-fry-cards`。
