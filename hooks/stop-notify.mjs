#!/usr/bin/env node
/**
 * 🕊️ feige-fry-cards Stop hook：ZCode 任务收尾时，把本会话的摘要战报卡发进飞书群。
 *
 * 开关（默认关，对齐 zcode-feishu-card 的 stop_hook_notify）：
 *   FEIGE_HOOK_NOTIFY=1  或插件 userConfig hook_notify=true（注入为
 *   ZCODE_USER_CONFIG_HOOK_NOTIFY）。未开启直接 exit 0。
 *
 * 调试：FEIGE_DRY_RUN=1 时给 feige 命令追加 --dry-run，并把命令行与 feige 输出
 *   打到 stderr，不发网络请求。
 *
 * rollout 目录：ROLLOUT_DIR > ZCODE_USER_CONFIG_ROLLOUT_DIR > ~/.zcode/cli/rollout
 *   （环境变量覆盖给离线测试用）。
 *
 * 会话文件定位：payload.session_id 命中文件名（model-io-sess_<id>.jsonl）就用它；
 *   匹配不上则兜底取目录里 mtime 最新的文件——Stop 事件刚收尾，最新文件就是本会话
 *   （并发多会话下可能取错，此时战报仍落在最新活跃的会话上，不至于发空卡）。
 *
 * 协议：stdin 读一个 JSON 事件，stdout 不写内容，诊断只走 stderr。
 * 任何异常 exit 0——通知 hook 绝不能阻塞或拖死 ZCode。
 */

import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, openSync, readdirSync, readFileSync, renameSync, statSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

const TEASER_CHARS = 300; // 摘要是战报不是镜像：正文 ≤300 字

const log = (msg) => {
  try { process.stderr.write(`[feige] ${msg}\n`); } catch { /* stderr 关了也不许炸 */ }
};

// ── 主流程 ───────────────────────────────────────────────────────────────────

async function main(raw) {
  let input = {};
  try { input = raw.trim() ? JSON.parse(raw) : {}; } catch { /* 脏 payload 也照常跑 */ }

  const rolloutDir =
    process.env.ROLLOUT_DIR ||
    process.env.ZCODE_USER_CONFIG_ROLLOUT_DIR ||
    join(homedir(), ".zcode", "cli", "rollout");
  if (!existsSync(rolloutDir)) {
    log(`rollout dir not found: ${rolloutDir}`);
    return;
  }

  const file = pickSessionFile(rolloutDir, String(input.session_id || ""));
  if (!file) {
    log("no rollout files found");
    return;
  }

  const summary = summarizeSession(file);
  if (!summary) {
    log(`[${file}] no usable model_io line (no finishReason=stop yet?)`);
    return;
  }

  const feigePy = fileURLToPath(new URL("../feige.py", import.meta.url));
  // 一律 --flag=value：值以 "-" 开头（如 "-修复登录"）时 argparse 会误当成选项而报错
  const args = [
    feigePy, "send",
    `--title=${summary.title}`,
    `--body=${summary.body}`,
    "--status=ok",
    `--project=${summary.project}`,
    `--model=${summary.model}`,
    `--thinking=${summary.thinking}`,
    `--tools=${summary.tools}`,
    `--context=${summary.context}`,
    `--tokens=${summary.tokens}`,
    `--elapsed=${summary.elapsed}`,
    // 有路由文件按项目 fanout，没有则退化为默认单渠道
    "--route",
    // 去抖键：FEIGE_DEBOUNCE_SECONDS>0 时同会话连续收尾只发最后一张
    `--debounce-key=zcode:${input.session_id || basename(file)}`,
  ];
  const dryRun = /^(1|true|yes)$/i.test(process.env.FEIGE_DRY_RUN || "");
  if (dryRun) args.push("--dry-run");

  // 密钥/userConfig 注入子进程：真实环境变量优先，插件 userConfig 兜底
  // （与 bridge 的 session-start.mjs 同一口径；hook 拿到的是 ZCode 进程启动时的
  //  陈旧环境，插件 Settings 里填的值往往更新，所以两条路都要走）
  const childEnv = {
    ...process.env,
    FEISHU_CARD_WEBHOOK: process.env.FEISHU_CARD_WEBHOOK || process.env.FEISHU_WEBHOOK_URL
      || process.env.ZCODE_USER_CONFIG_WEBHOOK_URL || "",
    FEISHU_WEBHOOK_SECRET: process.env.FEISHU_WEBHOOK_SECRET
      || process.env.ZCODE_USER_CONFIG_WEBHOOK_SECRET || "",
    FEISHU_APP_ID: process.env.FEISHU_APP_ID || process.env.ZCODE_USER_CONFIG_APP_ID || "",
    FEISHU_APP_SECRET: process.env.FEISHU_APP_SECRET || process.env.ZCODE_USER_CONFIG_APP_SECRET || "",
    FEISHU_BASE_URL: process.env.FEISHU_BASE_URL || process.env.ZCODE_USER_CONFIG_BASE_URL || "",
    FEISHU_NOTIFY_CHAT_ID: process.env.FEISHU_NOTIFY_CHAT_ID || process.env.ZCODE_USER_CONFIG_NOTIFY_CHAT_ID || "",
    FEIGE_LLM_API_KEY: process.env.FEIGE_LLM_API_KEY || process.env.ZCODE_USER_CONFIG_LLM_API_KEY || "",
    FEIGE_LLM_BASE_URL: process.env.FEIGE_LLM_BASE_URL || process.env.ZCODE_USER_CONFIG_LLM_BASE_URL || "",
    FEIGE_LLM_MODEL: process.env.FEIGE_LLM_MODEL || process.env.ZCODE_USER_CONFIG_LLM_MODEL || "",
    FEIGE_LLM_MAX_CHARS: process.env.FEIGE_LLM_MAX_CHARS || process.env.ZCODE_USER_CONFIG_LLM_MAX_CHARS || "",
    FEIGE_SUMMARIZE: process.env.FEIGE_SUMMARIZE || process.env.ZCODE_USER_CONFIG_SUMMARIZE || "",
  };

  if (dryRun) {
    // dry-run 是调试模式：同步执行并把命令行 + 卡片 JSON 打到 stderr，便于断言
    const prettyArgs = args.map((a) => (a.includes(" ") ? `"${a}"` : a)).join(" ");
    log(`dry-run command: python ${prettyArgs}`);
    const r = spawnPython(args, { sync: true, env: childEnv });
    if (r) log(`dry-run output:\n${(r.stdout || "").toString().trim() || "(empty)"}`);
    return;
  }

  // 正式路径：detached + unref + stdio ignore，hook 立刻退出，绝不阻塞 ZCode
  spawnPython(args, { sync: false, env: childEnv });
}

/** 同步探测可用解释器：依次试 python、py -3，返回 [命令, 前置参数] 或 null。
 *  必须同步：异步 spawn 的 ENOENT 要到下一个 tick 才触发，而 hook 随后立刻
 *  process.exit(0)，基于 error 事件的回退永远跑不到。 */
function resolvePython(env) {
  for (const [cmd, pre] of [["python", []], ["py", ["-3"]]]) {
    const r = spawnSync(cmd, [...pre, "--version"], { env, windowsHide: true, timeout: 5000 });
    if (!r.error && r.status === 0) return [cmd, pre];
  }
  return null;
}

/** dry-run 用同步版好抓输出；正式路径 detached 起子进程后立刻返回。 */
function spawnPython(args, { sync, env }) {
  const py = resolvePython(env);
  if (!py) { log("python not found (tried python, py -3)"); return null; }
  const [cmd, pre] = py;
  if (sync) {
    const r = spawnSync(cmd, [...pre, ...args], { env, encoding: "utf8", windowsHide: true });
    if (r.error) { log(`spawn failed: ${r.error.message}`); return null; }
    if (r.status !== 0) log(`feige exited ${r.status}: ${(r.stderr || "").trim()}`);
    return r;
  }
  // stderr 落日志文件：后台发送失败时还有据可查（口径同 feige.open_log_file）
  const logPath = env.FEIGE_LOG_FILE || join(tmpdir(), "feige.log");
  let logFd = "ignore";
  try {
    if (existsSync(logPath) && statSync(logPath).size > 1_000_000) renameSync(logPath, `${logPath}.1`);
    logFd = openSync(logPath, "a");
  } catch { /* 日志开不了也照发 */ }
  const child = spawn(cmd, [...pre, ...args],
    { env, detached: true, stdio: ["ignore", "ignore", logFd], windowsHide: true });
  child.on("error", (e) => log(`spawn failed: ${e.message}`));
  child.unref();
  if (typeof logFd === "number") closeSync(logFd);
  log(`feige spawned via ${cmd} (pid ${child.pid}), log: ${logPath}`);
  return child;
}

// ── rollout 解析（口径同 zcode-feishu-bridge/bridge.py，JS 移植） ────────────

function pickSessionFile(dir, sessionId) {
  let names;
  try {
    names = readdirSync(dir).filter((n) => n.startsWith("model-io-sess_") && n.endsWith(".jsonl"));
  } catch { return ""; }
  if (!names.length) return "";
  if (sessionId) {
    const hit = names.find((n) => n.includes(sessionId));
    if (hit) return join(dir, hit);
  }
  // 兜底：mtime 最新 = 刚收尾的本会话
  let best = "", bestMtime = -1;
  for (const n of names) {
    try {
      const m = statSync(join(dir, n)).mtimeMs;
      if (m > bestMtime) { best = n; bestMtime = m; }
    } catch { /* 文件被轮转走了就跳过 */ }
  }
  return best ? join(dir, best) : "";
}

// 辅助模型调用（压缩摘要/标题生成）会把样板文本记成 response.text，绝不能当正文渲染
const JUNK_RES = [
  /^\s*-?\s*Is a git repository/, // env-context 回显
  /^\s*<analysis>/,               // 会话压缩摘要
  /^\s*\{.*\}\s*$/s,              // 裸 JSON（标题生成等）
];
const ENV_SIGNATURES = ["Is a git repository", "Platform:", "Shell: Git", "OS Version:", "You are powered by"];

function isJunkText(t) {
  if (!t.trim()) return false;
  if (JUNK_RES.some((re) => re.test(t))) return true;
  const head = t.slice(0, 400);
  return ENV_SIGNATURES.filter((s) => head.includes(s)).length >= 2;
}

function teaser(text) {
  text = (text || "").trim();
  return text.length <= TEASER_CHARS ? text : text.slice(0, TEASER_CHARS).trimEnd() + " ……";
}

// 正文总结（口径同 adapters/common.body_for_card）：启用 LLM 总结时传更长原文，
// 由后台 feige send 进程调 LLM 浓缩；未启用保持 teaser 300 字老口径。
const FULL_BODY_CHARS = 6000;

function readSettings() {
  try {
    return JSON.parse(readFileSync(join(homedir(), ".feige-settings.json"), "utf8"));
  } catch { return {}; }
}

function summarizeEnabled() {
  const key = (process.env.FEIGE_LLM_API_KEY || String(readSettings().llm_api_key || "")).trim();
  const off = (process.env.FEIGE_SUMMARIZE || String(readSettings().summarize ?? "")).trim().toLowerCase();
  return !!key && !["0", "false", "no", "off"].includes(off);
}

function bodyForCard(text) {
  text = (text || "").trim();
  if (!text) return "……（本轮无文本输出）";
  if (!summarizeEnabled()) return teaser(text);
  return text.length <= FULL_BODY_CHARS ? text : text.slice(0, FULL_BODY_CHARS).trimEnd() + " ……";
}

function compact(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}m`;
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function fmtElapsed(seconds) {
  return seconds < 60 ? `${seconds.toFixed(1)}s`
    : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}

function projectFromPath(p) {
  p = p.trim().replace(/[\\/]+$/, "").replace(/\//g, "\\");
  for (const marker of ["GitHub Files\\", "workspace\\"]) {
    const i = p.lastIndexOf(marker);
    if (i >= 0) {
      const seg = p.slice(i + marker.length).split("\\")[0];
      if (seg && !seg.endsWith(":")) return seg;
    }
  }
  const seg = p.split("\\").pop();
  return seg && seg.length > 1 && !seg.endsWith(":") ? seg : "";
}

/** 统计口径：FEIGE_STATS_SCOPE / 插件设置 > ~/.feige-settings.json > "turn"（单轮）。 */
function readStatsScope() {
  const v = (process.env.FEIGE_STATS_SCOPE || process.env.ZCODE_USER_CONFIG_STATS_SCOPE || "")
    .trim().toLowerCase();
  if (v === "turn" || v === "session") return v;
  const s = String(readSettings().stats_scope || "").trim().toLowerCase();
  return s === "turn" || s === "session" ? s : "turn";
}

/** 该 model_io 请求的最新一条消息 role 为 user 时，视作一轮的开始；logs 拿不到 messages 就返回 ""。 */
function lastMsgRole(entry) {
  const msgs = ((((entry.request || {}).body) || {}).messages) || [];
  const m = Array.isArray(msgs) ? msgs[msgs.length - 1] : null;
  return m && typeof m.role === "string" ? m.role : "";
}

function findProject(entry) {
  // 权威来源：系统提示里的 `- Primary working directory:`，每个请求都带着
  const req = entry.request || {};
  const sysFields = [req.system, (req.body || {}).system];
  for (const sysField of sysFields) {
    const parts = Array.isArray(sysField) ? sysField : [sysField];
    for (const part of parts) {
      const c = typeof part === "object" && part !== null ? part.text : part;
      if (typeof c !== "string") continue;
      const m = c.match(/Primary working directory:\s*([^\r\n]+)/);
      if (m) return projectFromPath(m[1]);
    }
  }
  return "";
}

/** 单轮口径统计 + 最后一条 finishReason=="stop" 行的正文摘要。

分轮依据：request.body.messages 最新一条 role=="user" 的 model_io 行 = 一轮的开始
（rollout 拿不到 messages 或 scope=session 时退化为全文件累计）。 */
function summarizeSession(file) {
  const ctxTotal = Number(
    process.env.FEIGE_CONTEXT_TOTAL || process.env.ZCODE_USER_CONFIG_CONTEXT_TOTAL || 0,
  ) || 1_000_000;

  let firstAt = "", lastAt = "", model = "";
  let reasoningTurns = 0, toolsTotal = 0, tokensTotal = 0, ctx = 0, project = "";
  let turnReasoning = 0, turnTools = 0, turnTokens = 0, turnStartAt = "", sawTurnStart = false;
  let stopText = "", foundStop = false;

  let lines;
  try { lines = readFileSync(file, "utf8").split(/\r?\n/); } catch { return null; }
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let entry;
    try { entry = JSON.parse(trimmed); } catch { continue; } // 脏行跳过，fail-open
    if (entry.type && entry.type !== "model_io") continue;
    const stype = ((entry.request || {}).headers || {})["x-zcode-session-type"] || "";
    if (stype && stype !== "main") continue; // 子代理/内部会话不进战报

    const resp = entry.response || {};
    if (!project) project = findProject(entry);

    const at = entry.completedAt || "";
    if (lastMsgRole(entry) === "user") {
      // 新一轮开始：本轮统计清零重置（这行的响应属于新轮，先清后加）
      sawTurnStart = true;
      turnReasoning = 0; turnTools = 0; turnTokens = 0;
      if (at) turnStartAt = at;
    }

    const usage = resp.usage || {};
    ctx = Number(usage.inputTokens) || ctx;
    const out = Number(usage.outputTokens) || 0;
    tokensTotal += out; turnTokens += out;
    if (String(resp.reasoningText || "").trim()) { reasoningTurns += 1; turnReasoning += 1; }
    const tools = (resp.toolCalls || []).filter((t) => t && t.name).length;
    toolsTotal += tools; turnTools += tools;
    model = (entry.model || {}).modelId || model;
    if (at) { firstAt = firstAt || at; lastAt = at; }

    if (resp.finishReason === "stop") {
      foundStop = true;
      const text = String(resp.text || "").trim();
      stopText = isJunkText(text) ? "……（本轮无文本输出）" : bodyForCard(text);
    }
  }
  if (!foundStop) return null;

  const perTurn = readStatsScope() === "turn" && sawTurnStart;

  let elapsed = "";
  try {
    const t0 = new Date((perTurn && turnStartAt) || firstAt).getTime(), t1 = new Date(lastAt).getTime();
    if (t1 >= t0) elapsed = fmtElapsed((t1 - t0) / 1000);
  } catch { /* 时间戳脏了就不带耗时 */ }

  const pct = ctx ? Math.min((ctx / ctxTotal) * 100, 100) : 0;
  return {
    title: `${project ? project + " · " : ""}ZCode 任务完成`,
    body: stopText || "……（本轮无文本输出）",
    project,
    // 约定：CLI/Mirasim 场景模型名前带工具名
    model: model ? `zcode · ${model}` : "zcode",
    thinking: perTurn ? turnReasoning : reasoningTurns,
    tools: perTurn ? turnTools : toolsTotal,
    context: ctx ? `${compact(ctx)}/${compact(ctxTotal)} (${pct.toFixed(0)}%)` : "",
    tokens: perTurn ? turnTokens : tokensTotal,
    elapsed,
  };
}

// ── 驱动（放文件末尾：函数引用的 const 必须先完成初始化） ─────────────────────

const enabled = /^(1|true|yes)$/i.test(
  process.env.FEIGE_HOOK_NOTIFY || process.env.ZCODE_USER_CONFIG_HOOK_NOTIFY || "",
);
if (!enabled) process.exit(0);

let raw = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) raw += chunk;

try {
  await main(raw);
} catch (err) {
  log(`stop-notify failed (fail-open): ${err?.message || err}`);
}
process.exit(0);
