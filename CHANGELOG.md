# 更新日志

本项目所有值得关注的变更都会记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [2.3.2] - 2026-11-26 — 🕊️ 图标源图统一降到 256×256，上传体积近乎减半

### Changed

- **`vendor-logos` 20 张 + `agent-logos` 5 张全部由 640×640 重采样为 256×256**：卡片上
  图标最大渲染尺寸远小于 640，源图分辨率纯属上传负担。等比 + 预乘 alpha Lanczos 高质量
  重采样，仍是 8 位 P 模式调色板 PNG（行滤波 0、`tRNS` 透明索引），配色与图源一律不动：
  瓦片家族的中性底 `(72,75,84)` 与圆角/inset 几何按 0.4× 等比缩放，Claude Code 品牌橙
  `#D97757`、gemini 四色渐变逐张按「误差达标里最小」的色数档位保留（多数 64 色，
  doubao / hunyuan / xiaomimimo 96 色，minimax 192 色，gemini / codebuddy 256 色）。
  25 张合计 **231.3 KB → 121.0 KB（−47.7%）**。
- **逐张像素复核（纯标准库读 `PLTE`+`IDAT`，测试不引入 Pillow）**：尺寸 256×256、主色与
  缩放前一致、白字形图标在浅色面板上的深色像素占比仍过守卫（agent >0.5 / vendor >0.2）、
  两目录零重名；另比对未量化 Lanczos 参照物，可见像素平均 RGB 误差 ≤0.6（gemini 渐变
  1.48 为全库最大）。
- **上传缓存键结构不变**（`base|app|文件名|sha256[:12]`）：像素换了 → 摘要变了 → 每张用过
  的 logo 会重新上传一次，这是一次性代价，之后照旧命中缓存。

## [2.3.1] - 2026-11-26 — 🕊️ Studio 弹层自己定死几何 + Claude Code 图标改品牌橙

### Fixed

- **「具体设置」弹层排版错乱、字段跑到框外**：`showModal()` 不可用（内嵌浏览器 / 旧版
  Chromium 里会缺失或抛错）时走的是非模态兜底，浏览器只给 `open` 属性、不给任何限高与
  居中，弹窗按普通块排版一路长到视口外，保存条被内容推着浮在半空，下半截字段（飞书域名、
  10Router 群 chat_id、Webhook）露在框外。现在弹层自己定死 `position:fixed; inset:0;
  margin:auto` + `max-height`，头固定、正文内部滚动、保存条 `sticky` 贴底，两条路径几何
  完全一致；兜底路径另补一层自有遮罩，页面正文不再从缝里透出来。
- **三个渠道弹层同时叠在页面上**：上面那组选择器优先级高于浏览器默认的
  `dialog:not([open]){display:none}`，又给出了 `display`，于是「关着」的弹窗也被排出来、
  相互叠压（打开飞书时 Telegram 压在上面）。改为 `:not([open])` 显式不显示、`[open]` 才
  `display:flex`。
- **收起态折叠区依赖引擎行为**：新 Chrome 靠 `::details-content{content-visibility:hidden}`
  隐藏收起内容，内嵌/旧引擎不认，收起的 Webhook 字段仍占位并绘制。加
  `details.fold-card:not([open])>.fold-body{display:none}`，不依赖引擎。
- **每次点击都抛 `SyntaxError`**：`.menu-select` 的 document 点击监听写成
  `e.target.closest(box)`，`closest` 只接受选择器字符串，传 Element 抛
  「`[object HTMLSpanElement]` is not a valid selector」，等于每次点页面都在控制台报错。
  改为 `box.contains(e.target)`。
- **宿主 Claude Code 图标是黑白方块**：`agent-logos/claude-code.png` 原本是深灰瓦片 +
  白色机器人，白底面板上是「黑块 + 白机器人」。换成 Anthropic 品牌橙 `#D97757` 瓦片 +
  白色机器人、四角透明，仍为 640×640、64 色 P 模式，与同目录其他 agent 图标同规格。
  上传缓存键早已含 `sha256[:12]`，已发过卡的机器会重新上传新图。

## [2.3.0] - 2026-11-26 — 🕊️ 模型图标认得出路由：别名按智能体显示，未识别兜底宿主图

### Added

- **未识别模型兜底智能体图标**：面板头左侧原来只在命中厂商家族时才有图标，代理池 /
  自建网关的模型名（如 `chat-mode-pool`）一律空着。现在厂商未命中时改用**宿主/智能体
  图标**兜底——按模型串 `宿主 · 模型` 的前缀或显式 host 判定 Claude Code / Codex /
  ZCode / kimi-code / Antigravity，新增 `assets/agent-logos/` 5 张资产。命中厂商时
  口径不变（厂商图 + 官方名）；`vendor_logo_enabled` / `FEIGE_VENDOR_LOGO` 关掉时
  依旧一个图标都不出。Studio 预览页同步（模型速选新增 `chat-mode-pool` 一档可直接
  预览兜底效果），`/assets/agent-logos/` 与厂商目录同等做路径越界校验。
- **路由别名模型按路由目标显示智能体图标**：网关的池化入口（`chat-mode-pool`、
  `openai/chat-mode-pool`、上游真名 `qwork-claude-chat-mode-pool` 等）名字里常带厂商词，
  按厂商规则会先匹配出 OpenAI / Claude 的厂商 logo，而真正执行的是卡片宿主。现在这类
  别名**先于厂商判定**被识别（`is_alias_model`），面板头显示路由到的智能体图标与名称
  （`Claude Code` / `Codex` …），展开面板的「模型：」同时保留原串便于回查网关。路由目标
  取值优先级：模型串显式标记（`alias:claude-code` / `→ codex`）> 发卡时上报的宿主
  （适配器元数据，即真实执行者）> `宿主 · ` 前缀；**都解析不出时回退厂商 logo**，
  认不出也不会空着。具体厂商模型（`deepseek-v4.1-flash`、`claude-sonnet-4`）不受影响。

### Fixed

- **7 张厂商 Logo 白底卡片上看不见**：`openai` / `moonshot` / `grok` / `dotsstudio` /
  `upstage` / `xiaomimimo` / `kimi` 原本取的是 lobehub 的 dark 变体（纯白或近纯白），画在
  浅色面板头上等于没有图标——用户看到的「模型那头空着」有一半是这个原因。全部换成
  白底可见的版本，并加测试锁住「每张资产都必须有足够深色像素」。
- **Logo 上传缓存改按内容失效**：`~/.feige-logo-cache.json` 的键原来只有
  `base|app|文件名`，资产像素换过仍会命中旧 `image_key`，上面那条修复在已发过卡的
  机器上永远显不出来。键里加进文件内容摘要，换图即重传。
- `logo_asset_path` 只接受纯文件名，带路径分隔符或 `..` 的外部片段一律拒绝。

## [2.2.0] - 2026-11-26 — 🕊️ 配置页真瀑布流 + 下拉不被裁 + 面板头去重复宿主前缀

### Changed

- **Studio 配置页改按宽度自适应的真瀑布流**：原来的 CSS 多列（`columns:420px`）是
  顺序填充——一张高卡放不下就整张断到下一列，前一列底下吊出大片空白，窄栏还会横向
  溢出顶出滚动条。现改为等宽 flex 列 + 脚本按「当前最矮列」装载卡片，列数随宽度自适应
  （≥380px 一列，1~3 列），列高保持自然、整页高度取最高列；脚本不执行时退化为单列
  纵向堆叠，同样不留空洞。宽窄两档、深浅主题实测无横向滚动条。
- **下拉菜单不再被父容器裁切**：菜单选择器与模型拉取列表改 `position:fixed` 锚定到
  按钮的视口坐标（脚本实时计算），靠近视口底缘自动向上翻，宽度跟随按钮但受视口限制；
  滚动时菜单跟随按钮、按钮滚出视口自动收起。隐藏 input + `change` 冒泡的契约不变，
  脏值检测 / 即时预览 / 保存链路照常。
- **面板头模型名默认不带宿主前缀**：卡片标题已含「项目 · 宿主」，面板头再写一遍
  `claude-code · ` 属同一张卡重复三遍。现在面板头只留模型官方名（`Qwen 4-max`），
  设置键 `panel_host_prefix` / 环境变量 `FEIGE_PANEL_HOST_PREFIX` 显式开启才还原旧口径；
  默认关让旧配置（不写键）自然落入新行为。统计脚注与钉钉 / Telegram 静态版式不受影响。

### Added

- **Studio 两处新入口**：配置页「交互与长连接」卡新增「面板头模型名带宿主前缀」开关；
  预览页新增同名「临时预览」三态切换（跟随设置 / 不带 / 带），不写设置文件即可对比。

## [2.1.0] - 2026-11-26 — 🕊️ Studio 三档上下文样式 + 渠道菜单收拢 + 具体设置弹窗 + 状态灯

### Added

- **面板头上下文样式可切换（三档，对齐兄弟项目）**：统一折叠面板头的上下文水位
  支持三档呈现，口径与 `claw-fry-cards` 的 `panel.contextDisplayMode` /
  `hermes-fry-cards` 的 `display.context_display_mode` 一致——
  `text_bar` 文本+进度条 `165.4k/1.0m [█▓▒░] 17%`（默认）、`text` 纯文本
  `165.4k/1.0m (17%)`、`bar` 纯进度条 `[█▓▒░] 17%`。设置键 `context_style` /
  环境变量 `FEIGE_CONTEXT_STYLE`（非法值回落默认，旧值 `plain` 等价 `text`）；
  Studio 配置页新增样式菜单，预览页带「临时预览」三档切换（不写设置文件即可对比）。
  只切统一面板头，统计脚注与钉钉 / Telegram 静态版式保持原样。

### Changed

- **Studio 配置页渠道收拢为菜单、具体设置改弹窗**：飞书 / 钉钉 / Telegram 三张渠道卡
  合并为一张「渠道」菜单卡——每渠道一行（图标 + 说明 + 配置状态 + 「具体设置」），
  点开弹窗编辑凭据；配置页更紧凑，保存作用域一目了然。
- **配置状态改用状态灯**：配置页各字段徽章、渠道行与状态页凭据表改以状态灯示意——
  灰 = 未配置 / 默认、绿 = 已配置（设置文件）、橙 = 环境变量覆盖，来源标注照常保留；
  密钥与 webhook 仍永不回显。
- **Studio 下拉选择器统一为菜单控件**：配置页与预览页的原生 `<select>` 换成统一样式的
  菜单选择控件（隐藏 input + 自绘列表，深浅主题一致），变更事件照常冒泡——脏值检测、
  即时预览与保存链路不受影响。

## [2.0.0] - 2026-11-26 — 🕊️ 家族卡片规范对齐 + 折叠面板 + 交互式飞书长连接续跑 + feige Studio 2.0

### Added

- **底部统一折叠面板（`collapsible_panel`）**：对齐 `hermes-fry-cards` / `claw-fry-cards`
  CardKit v2.0 规范，顶层保持精简战报摘要，底部新增默认折叠的 `unified_panel` 面板
  （绿/红/蓝状态边框、上下文密度进度条 `█▓▒░`）承载详细正文、工具调用摘要与安全脱敏的会话信息。
- **厂商家族 Logo + 官方模型名展示**：引入 20 套 `@lobehub/icons-static-png` 厂商 Logo 资产
  （`assets/vendor-logos/`）与官方模型名映射规则（如 `tencent/hy4-flash:free` → `Hy4 flash`）。
  `feishu-cardkit` 渠道自动上传并本地缓存 `image_key`（`custom_icon` 左置）；`feishu-webhook`
  及未命中/失败场景保持干净纯标题 + 旋转箭头，Studio 预览页同步支持本地静态 Logo 渲染。
- **完整交互式卡片与飞书长连接（`feige_interactive.py`）**：
  - `feishu-cardkit` 完成态卡片在绑定本地会话上下文后渲染「追问 / 继续指令」表单及防误触快捷按钮
    （`▶️ 继续执行`、`🧪 跑测试核验`，均带飞书原生二次确认弹窗）；
  - 接入飞书长连接 `WSClient` 接收 `card.action.trigger`（可随 `python webui.py start`
    守护进程自动保活，亦支持 `python feige.py listen` 独立前台监听）；3 秒内同步返回
    Toast + 蓝色「续跑处理中」中间态卡片防重复提交；
  - 敏感路径（`cwd`）与原始 `session_id` 仅保存于本机 `~/.feige-sessions.json`，卡片公开字段
    只暴露随机不透明令牌 `ctx_id`；校验操作人白名单（兼容 Schema 2 `open_id`/`user_id`）及群聊
    一致性后，对支持无头续跑的宿主（Claude Code / Codex CLI / ZCode / kimi-code）后台续跑并
    回写更新原卡（原卡过期/失败时自动退化为回复新卡）；对不支持宿主（如 Antigravity）给出清晰提示。
  - 新增 `pyproject.toml` 声明可选依赖 `lark-oapi>=1.4.0`（`[interactive]`），未安装时优雅降级，
    发卡核心与其余渠道仍保持纯标准库零依赖与 fail-open。
- **Studio 设置页同步升级（`webui.py`）**：飞书卡片新增交互式开关、长连接开关、厂商 Logo 开关、
  折叠面板默认展开开关、操作人白名单、无头续跑超时配置；状态页新增 WS 长连接状态与本机宿主
  CLI 续跑能力探测及「同步长连接状态」操作；预览页同步还原折叠面板、厂商 Logo 与交互组件。

### Changed

- **终态卡信息去重**：完成/报错态卡片默认不再输出底部 `hr` + 统计脚注行——统一折叠面板头
  已承载同一组统计，重复输出刷屏；面板头字段默认去掉 `project`（标题已含项目与宿主）。
  新增 `FEIGE_FOOTER_ENABLED`（设置键 `footer_enabled`，默认关）可恢复脚注行，
  `FEIGE_PANEL_FIELDS`（设置键 `panel_fields`）与 `FEIGE_FOOTER_FIELDS` 拆分为面板/脚注
  两套独立字段配置（旧 `footer_fields` 配置保持兼容、不迁移）；钉钉 / Telegram 静态版式
  与 running 态不受影响。Studio 配置页两套字段各自成组可拖拽排序，预览页同步。
- **正文总结更稳**：总结 LLM 请求超时 20s → 45s（只在后台发送进程里等，宿主 hook 前台
  零阻塞不变），缓解免费/慢速池偶发超时导致顶层回落截断原文；失败仍 fail-open 且日志可辨。

### Fixed

- **README 密钥占位**：文档示例中的真实网关密钥替换为占位符（该密钥已进入历史提交，
  建议到网关侧轮换）。

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

[Unreleased]: https://github.com/techysy/feige-fry-cards/compare/v2.3.1...HEAD
[2.3.1]: https://github.com/techysy/feige-fry-cards/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/techysy/feige-fry-cards/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/techysy/feige-fry-cards/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/techysy/feige-fry-cards/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/techysy/feige-fry-cards/compare/v1.2.0...v2.0.0
[1.2.0]: https://github.com/techysy/feige-fry-cards/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/techysy/feige-fry-cards/compare/v1.1.0...v1.1.1
<!-- v1.0.1 当年只更了清单没打 tag，compare 从 v1.0.0 起 -->
[1.1.0]: https://github.com/techysy/feige-fry-cards/compare/v1.0.0...v1.1.0
[1.0.1]: https://github.com/techysy/feige-fry-cards/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/techysy/feige-fry-cards/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/techysy/feige-fry-cards/releases/tag/v0.1.0
