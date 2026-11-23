# 更新日志

本项目所有值得关注的变更都会记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.2.0] - 2026-11-23 — 🕊️ Antigravity CLI（反重力）适配器

### Added

- **Antigravity CLI（agy）适配器**：新增 `adapters/antigravity/stop_notify.py`，agy 原生
  Stop hook（`<workspace>/.agents/hooks.json` 或 `~/.gemini/config/hooks.json`）→ 会话
  收尾战报卡。hook payload（camelCase：`conversationId`/`transcriptPath`）与转录
  schema（`PLANNER_RESPONSE` 的 model/usage/thinking/tool_calls）均按本机 agy 1.5.11
  实证；💭/🔧/🎫/⏱️ 只算最后一轮，上下文水位 = input+cache_read（agy 不暴露
  cache_creation），模型窗口表含 gemini-3.8-flash / claude-sonnet-4 / gpt-oss-120b。
  `hooks/stop.py` 按 `conversationId` 或 `antigravity-cli` 转录路径识别分发；
  后台发送进程重读转录同样识别（feige.py）。

## [1.1.1] - 2026-11-22 — 🕊️ 面板导航对齐卡片 + 提示文案窄屏截断

### Fixed

- **导航栏对齐卡片**：胶囊导航左缘补上内容列 22px 内边距，与卡片左缘齐平，
  不再凸出一截。
- **提示文案窄屏自适应**：配置页顶部两行安全提示非宽屏单行截断（省略号），
  不再挤成四排占高度；≥1420px 宽屏恢复完整换行。

## [1.1.0] - 2026-11-22 — 🕊️ Studio 后台保活 + 宽屏瀑布流 + 总结独白防线

### Added

- **宽屏 3 列 + 瀑布流高度自适应**：内容列宽抽成 `--col-w` 变量，视口 ≥1420px 时
  放宽到 1400px，配置页卡片栅格（420px 下限自适应）自动从 2 列变 3 列，
  header/导航/内容同轴对齐；栅格改多列瀑布流——行式 grid 的行高被该行最高的卡
  撑开、矮卡下吊空白把整页顶出超一屏，现在每列独立堆叠、总高≈最高列，
  宽屏配置页收进约一屏；窄屏布局与老的 980px 口径不变。
- **Studio 后台保活**：`python webui.py start` 把面板拉成脱离终端的守护进程——
  终端/宿主关闭不再连带杀掉面板，worker 崩溃自动重启（存活 <3s 视为崩溃循环退避 5s），
  端口被其他实例接管时守护自动退出。配套 `stop`（按 pidfile 整树杀）/ `restart` /
  `status`（探活）；保活日志 `~/.feige-studio.log`。前台 `python webui.py` 老用法不变。
  守护进程树全程不落可见控制台窗口（每层 spawn 都带脱离标志——无控制台进程生出
  控制台程序时 Windows 会弹可见空窗）。
- **总结触发阈值（战报不是镜像）**：启用总结 LLM 后，正文超过「总结触发字数」
  （默认 300 字，`llm_min_chars` / `FEIGE_LLM_MIN_CHARS` 可调）就先浓缩成战报摘要，
  中等长度的收尾汇报不再逐字镜像上卡；设 `0` 回到「只在超渠道上限才总结」的旧口径。
  Studio「正文总结」卡与插件设置均新增该字段。

### Changed

- **版本号单一来源**：主插件版本号收进 `feige.__version__`，面板 `webui.VERSION`
  同源读取，`.claude-plugin` / `.zcode-plugin` 两份清单（plugin.json +
  marketplace.json）同步 1.1.0（zcode marketplace 此前漏更停 1.0.0），
  测试锁住五处一致防漂移。
- **主页去掉重复的群路由卡**：群路由已有独立页签，配置页底部不再挂入口卡，
  主页更紧凑。

### Fixed

- **总结模型的独白不再上卡（此地无银三百两）**：总结 LLM 返回不再整段镜像——
  系统要求摘要放进 `<summary>` 标签，标签外的复述/分析/字数检查一律丢弃；
  模型不配合标签约定时退化为逐行剥离复述要求的行与「让我组织一下」「检查字数」
  式独白行，剥完为空退化硬截断原文。`max_tokens` 余量放宽（碎碎念模型会先烧掉
  一段预算，避免摘要中途被截），截断输出（`finish_reason=length`）落日志可查。
- **战报落后一个卡片**：适配器正文与统计口径对齐——只取**当前轮**的最后收尾文本，
  轮边界清零、绝不回落上一轮旧文（此前当前轮被打断/纯工具收尾/宿主提前触发 Stop 时，
  战报会发上一轮的内容）；Claude Code / kimi-code / ZCode 三适配器同修。
  另带转录的宿主（Claude Code / kimi）把转录路径交给后台发送进程，开卡前等
  `FEIGE_READ_BODY_DELAY`（默认 1.5s）重读一次转录——宿主落盘略滞后于 Stop 触发时，
  晚读到的那份才是收尾后的完整正文与统计。
  Studio 品牌区图标恢复 🕊️ emoji。

## [1.0.1] - 2026-10-19 — 🕊️ 每群自动开启 + 按项目接收群

### Added

- **每群自动开启（per-project 自动战报开关）**：路由文件里配了目标的项目，即使没开
  全局 `FEIGE_HOOK_NOTIFY`，每轮收尾也会自动发卡；没配路由的项目照旧不发。
  全局开关仍是硬闸（显式 `FEIGE_HOOK_NOTIFY=0` / 插件 hook_notify 关闭时，
  未配路由的项目一律静默），不会出现「关不掉」。
  Studio「群路由」页每个项目新增「自动战报」勾选框，勾选结果存
  `~/.feige-settings.json` 的 `auto_notify_projects`，保存路由时一并写入。
- **按项目覆盖接收群**：设置文件支持 `"feishu_chat_id__<项目名>": "oc_xxx"`
  （环境变量同名为 `FEISHU_CHAT_ID__<项目名>`）。路由目标没写 `chat_id` 时落到
  该项目的群，没配项目键再落回全局默认群；目标里显式写的 `chat_id` 优先级最高。
  Studio 飞书渠道卡新增 10Router 群输入框。
- **模型在线拉取（对齐 ImgMark）**：feige Studio 总结 LLM 支持从 OpenAI 兼容服务的 `/models` 接口拉取可用模型列表；支持按厂商/前缀自动分组、上下文长度角标呈现、模糊搜索过滤，点击卡片即刻填入配置。

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

[Unreleased]: https://github.com/techysy/feige-fry-cards/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/techysy/feige-fry-cards/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/techysy/feige-fry-cards/compare/v1.1.0...v1.1.1
<!-- v1.0.1 当年只更了清单没打 tag，compare 从 v1.0.0 起 -->
[1.1.0]: https://github.com/techysy/feige-fry-cards/compare/v1.0.0...v1.1.0
[1.0.1]: https://github.com/techysy/feige-fry-cards/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/techysy/feige-fry-cards/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/techysy/feige-fry-cards/releases/tag/v0.1.0
