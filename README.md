# 飞鸽 Feige

> 飞鸽传书——把 agent 的战报投进任何群。

一个 **agent 侧的结果汇报插件**：任何编码 agent（ZCode、Claude Code、Codex……）的会话结束时，
自动生成**摘要结果卡片**，直发飞书/钉钉等群聊。不依赖任何 agent 产品的内置渠道，
卡片形态完全自控。

## 立项背景

对 Mirasim 的评估（2026-09）证实了两件事：

1. "agent 进展汇报到 IM 群"是被验证的标准需求——Mirasim 把它做成了内置的 7 渠道双向遥控；
2. 但内置渠道**不给你卡片形态和注入的控制权**（闭源固定适配器、无渠道插件 SDK），
   想要"自定义摘要卡片进群"，正确姿势是在 agent 侧挂 skill，走各平台 webhook 直发。

飞鸽就是第二条路。它的发卡核心抽自 [zcode-feishu-bridge](../zcode-feishu-bridge) 已验证的
CardKit 实现（流式卡片生命周期、`sequence` 递增、信封 body 等硬坑全趟过），
从"ZCode 日志 tail 守护进程"升级为**渠道无关、agent 无关的汇报层**。

## 与 zcode-feishu-bridge 的关系

| | zcode-feishu-bridge | 飞鸽 Feige |
|---|---|---|
| 定位 | ZCode 专属桥接守护进程 | 通用 agent 汇报插件 |
| 触发 | tail rollout 日志（被动） | skill / hook / CLI（主动），tail 只是入口之一 |
| 通道 | 飞书自建应用 | webhook 直发优先，多渠道可插拔 |
| 卡片 | CardKit 流式卡片 | 同一套核心，精简为"摘要 + 封卡统计" |

zcode-feishu-bridge 继续维护（ZCode 有官方 IM 通道前它仍是补位方案）；飞鸽是其发卡能力的
产品化抽取。两边共享设计不变量（见下）。

## 设计原则（继承自 bridge 的踩坑）

- **摘要是战报，不是镜像**：卡片正文截断（300 字级），全文永远留在 agent 客户端/官方通道。
- **统计行只在封卡出现**：进行中的卡不堆指标；综合面板是收尾脚注，单次呈现。
- **fail-open**：发卡失败绝不能拖垮 agent 会话；异常只落日志。
- **密钥只走环境变量**：`FEISHU_APP_ID/SECRET`、webhook URL 一律不入库、不进日志。
- **Python 标准库 only**，单文件核心优先；文档与注释用中文。

## 架构

```
┌───────────── 入口（可插拔） ─────────────┐
│ skill 调用   hook 回调   CLI   tail 守护  │  ← agent 侧接入，各 agent 一层薄适配
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
- **渠道**决定"发到哪、什么形态"：每个渠道是一个薄 adapter，输入统一的卡片模型
  （标题 / 摘要正文 / 统计脚注 / 状态），输出各平台消息。
- 群路由：一份配置把「项目 → 群 webhook」映射起来，支持一个项目多群。

## 里程碑

- **M1 发卡核心**：从 bridge.py 抽出 CardKit + webhook 两条飞书路径为独立单文件库，
  提供 `feige send --title … --body … [--channel feishu-webhook]` CLI。
- **M2 ZCode 接入**：以 skill/hook 形式接入 ZCode，会话收尾自动发摘要卡，与现有
  bridge 守护进程并存切换。
- **M3 Claude Code / Codex 接入**：Claude Code 的 Stop hook、Codex 的 notify 机制，
  验证"agent 无关"成立。
- **M4 多渠道**：钉钉 webhook、Telegram Bot；群路由配置（项目 → 多群 fanout）。

## 命名

**飞鸽 Feige**：飞鸽传书，谐音飞书之"飞"；CLI 命令 `feige`。
备用名 `agent-herald`（若未来明显超出飞书生态再启用）。
