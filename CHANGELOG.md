# 更新日志

本项目所有值得关注的变更都会记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.0.0] - 2026-10-17 — 🕊️ 从首飞到 1.0

### Added

- **M6 一份插件四个宿主**：`.claude-plugin/` 清单（Claude Code 与 Codex 共用）+
  统一 Stop hook `hooks/stop.py`（识别 ZCode / Codex / Claude Code 后分发）+
  Codex notify 包装链（已有 notify 的用户可把原命令接在后面两边都跑）（`5708480`）
- **M7 kimi-code 接入**：第五个宿主 `adapters/kimi-code/stop_notify.py`
  （Stop payload + `wire.jsonl` 统计）；`~/.kimi-code/config.toml` 手挂，注册即开启
  （`1d352a4`）
- **M8 正文 LLM 总结**：正文超长不再直接硬截断——配置 OpenAI 兼容
  `chat/completions`（`FEIGE_LLM_API_KEY` / `_BASE_URL` / `_MODEL` / `_MAX_CHARS`）后，
  后台发送进程先把冗长收尾汇报浓缩成战报摘要，未配置/失败自动退化硬截断；
  dry-run/预览与宿主 hook 前台零网络阻塞；启用后各适配器放行 ≤6000 字原文
  给总结器（`c3d1c92`）
- **M9 feige Studio 设置页**：WebUI 重做成配置 / 预览 / 状态 / 群路由 / 接入自检
  五页签、分卡片表单 + 独立保存；渠道凭据、总结 LLM、行为开关全部可只配
  `~/.feige-settings.json`，保存即生效，不用再碰环境变量（`edab28a`）
- **收尾去抖**：`FEIGE_DEBOUNCE_SECONDS` + `--debounce-key`，同会话连续收尾
  只发最后一张卡（`e3d0d05`）
- **后台发送**：自动入口的发送走 detached 子进程，网络再慢也不阻塞 agent
  （`e3d0d05`）
- **签名体系**：飞书机器人「签名校验」（`FEISHU_WEBHOOK_SECRET`）与
  钉钉「加签」（`DINGTALK_SECRET`），路由目标可各自带 `secret`（`468b157`）
- **按渠道长度上限**：飞书/钉钉正文按 UTF-8 字节卡 15000、Telegram 整条按
  UTF-16 单元卡 4096（emoji 占 2），截正文保脚注（`468b157`）
- 自动入口一律带 `--route`：有路由文件按项目 fanout，没有则保持单渠道（`e3d0d05`）

### Changed

- 💭/🔧/🎫/⏱️ 统计切到**单轮口径**（默认 `stats_scope=turn`，`session` 可切回全程）：
  kimi 按 `turnId`（`2706d53`）、Claude Code 按最后一条真实用户提问分轮（`ec1c517`）
- 战报统计与 ZCode bridge 对齐：token 紧凑格式（`14.3k`）、🎫 输出 token 字段（`7db7342`）
- CardKit 结果卡精简为 2 次 API 调用（建终态卡 → 按引用发群，不再走流式空转）（`468b157`）
- 配置统一三级优先级：真实环境变量 > 插件 userConfig > 设置文件
  `~/.feige-settings.json`（`feige._env` 全渠道生效）（`c3d1c92` `edab28a`）
- README 按统一规范重写（徽章 / SVG 架构图深浅色适配 / 功能分组），并补齐
  Studio 设置页、正文 LLM 总结、环境变量参考三节使用指南（`236c779` `c045eb5`
  `6f2f742` `7974346`）
- `skills/feige` SKILL 同步配置口径与总结兜底说明（`7974346`）

### Fixed

- 审查修复批次（`159d8dd`）：
  - hook 解释器回退：`python` 不在 PATH 时自动试 `py -3`
  - WebUI 安全：Host 校验防 DNS rebinding、POST Origin/Referer 校验防 CSRF
  - 路由 `$ENV` 未设置时不再静默回退默认渠道（防止战报发错群），改为点名跳过
  - 正文以 `-` 开头发不出：命令行参数统一 `--flag=value` 写法
- `stats_footer` 对 `thinking`/`tools` 为 0 时不省略的存量问题（`1d352a4`）
- 两处 `context_window` 测试受 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 环境污染的
  隔离修复（`c3d1c92`）

### Security

- `redact` 全链路覆盖 LLM API key；设置文件中的密钥同样参与打码（`c3d1c92`）
- Studio 密钥输入框永不回显：只显「已配置 · 来源」徽章，留空 = 不修改、
  勾选「清除」才删除（`edab28a`）

## [0.1.0] - 2026-09-26 — 🕊️ 首飞

### Added

- **M1 发卡核心**：从 zcode-feishu-bridge 抽 CardKit 流式卡生命周期、从
  zcode-feishu-card 抽 webhook 双通道发送，合成独立单文件库 `feige.py`——
  统一卡片模型（标题 + 摘要正文 + 统计脚注 + 状态着色）、
  `feishu-webhook` / `feishu-cardkit` 双渠道自动选择、`feige send` CLI（`fd7e1db`）
- **M2 ZCode 接入**：Stop hook 收尾战报卡（默认关）、`.zcode-plugin/` 清单、
  `hooks/stop-notify.mjs`、`skills/feige/` 技能（`06df129`）
- **M3 Claude Code / Codex 适配器**：`adapters/claude-code/stop_notify.py`
  （transcript 统计）与 `adapters/codex/notify.py`（notify 事件 → teaser 卡），
  直接 import feige.py 验证「agent 无关」成立（`c1cd6b6`）
- **M4 多渠道 + 群路由**：新增 `dingtalk-webhook`（markdown 消息）与 `telegram`
  （Bot API 纯文本）渠道，扩至 4 个；`--route` 按项目 → 多目标 fanout，
  `$ENV` 引用防密钥固化（`bb7d7e1`）
- **M5 独立设置 WebUI**：`webui.py`（纯标准库 `http.server`，只绑 127.0.0.1），
  总览 / 群路由编辑 / 卡片预览 / 测试发送 / 接入自检，密钥永不回显（`ae4002a`）

### Docs

- 立项与 fry-cards 家族命名对齐（`8a7d583` `59667f4`）
- 联调排错指南：230002 与 `setx` 陈旧环境变量坑（`de97166`）
- 快速开始、使用指南目录锚链、家族表格绝对链接（`7b4945a` `a6878d4` `f3b6bff`）

[Unreleased]: https://github.com/techysy/feige-fry-cards/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/techysy/feige-fry-cards/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/techysy/feige-fry-cards/releases/tag/v0.1.0
