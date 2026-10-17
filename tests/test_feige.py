"""feige 回归测试（纯标准库 unittest，不发网络）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import feige  # noqa: E402
import webui  # noqa: E402


class StatsFooterTest(unittest.TestCase):
    def test_full(self):
        s = feige.stats_footer({"project": "p", "model": "vendor/m1", "thinking": 3,
                                "tools": 5, "context": "42%", "elapsed": "1m 2s"})
        self.assertEqual(s, "📦 p · m1 · 💭3 · 🔧5 · 42% · ⏱️ 1m 2s")

    def test_empty_fields_omitted(self):
        self.assertEqual(feige.stats_footer({"project": "", "thinking": 0}), "")
        self.assertEqual(feige.stats_footer(None), "")

    def test_running_card_has_no_footer(self):
        card = feige.build_card("t", "b", stats={"project": "p"}, status="running")
        self.assertEqual(len(card["body"]["elements"]), 1)


class RedactTest(unittest.TestCase):
    def test_patterns(self):
        s = feige.redact('tenant_access_token":"t-abcdef123456 bot123456789:AAAAAAAAAAAAAAAA')
        self.assertNotIn("t-abcdef123456", s)
        self.assertNotIn("AAAAAAAAAAAAAAAA", s)

    def test_configured_secret(self):
        with mock.patch.dict(os.environ, {"DINGTALK_WEBHOOK": "https://oapi/x?access_token=zzz999"}):
            self.assertEqual(feige.redact("boom https://oapi/x?access_token=zzz999"), "boom ***")


class RoutesTest(unittest.TestCase):
    DOC = {"routes": {
        "a": [{"channel": "feishu-webhook", "webhook": "$HOOK_A"}],
        "*": [{"channel": "telegram", "chat_id": "-100"}],
    }}

    def test_env_expansion(self):
        t = feige.resolve_routes("a", self.DOC, env={"HOOK_A": "https://h/a"})
        self.assertEqual(t, [{"channel": "feishu-webhook", "webhook": "https://h/a"}])

    def test_wildcard_fallback(self):
        self.assertEqual(feige.resolve_routes("zzz", self.DOC, env={})[0]["channel"], "telegram")

    def test_unset_env_marked_unresolved(self):
        t = feige.resolve_routes("a", self.DOC, env={})
        self.assertEqual(t[0]["_unresolved"], ["HOOK_A"])

    def test_unset_env_never_falls_back_to_default_webhook(self):
        """回归：$ENV 未设置时曾展开为空串，send_report 回退默认 webhook 发错群。"""
        env = {k: v for k, v in os.environ.items() if k != "HOOK_A"}
        env["FEISHU_CARD_WEBHOOK"] = "https://default/hook"
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(feige, "send_report") as sr:
            ok, detail = feige.send_routed("t", "b", project="a", doc=self.DOC)
        sr.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("$HOOK_A", detail)

    def test_missing_channel_log_has_no_values(self):
        doc = {"routes": {"a": [{"webhook": "https://secret/hook"}]}}
        with mock.patch.object(feige, "log") as lg:
            ok, _ = feige.send_routed("t", "b", project="a", doc=doc, dry_run=True)
        self.assertFalse(ok)
        self.assertNotIn("secret", " ".join(str(c) for c in lg.call_args_list))


class CliTest(unittest.TestCase):
    def test_body_starting_with_dash(self):
        """回归：--body "-修复登录" 被 argparse 当成选项；hook 改用 --body=... 形式。"""
        with mock.patch("builtins.print"):
            self.assertEqual(feige.main(["send", "--title=t", "--body=-修复登录", "--dry-run"]), 0)


class WebuiGuardTest(unittest.TestCase):
    P = 8787

    def allowed(self, method, host, origin="", referer=""):
        return webui.request_allowed(method, host, origin, referer, self.P) == ""

    def test_local_get(self):
        self.assertTrue(self.allowed("GET", "127.0.0.1:8787"))
        self.assertTrue(self.allowed("GET", "localhost:8787"))

    def test_dns_rebinding_host_rejected(self):
        self.assertFalse(self.allowed("GET", "evil.example:8787"))
        self.assertFalse(self.allowed("GET", ""))

    def test_same_origin_post(self):
        self.assertTrue(self.allowed("POST", "127.0.0.1:8787", origin="http://127.0.0.1:8787"))
        self.assertTrue(self.allowed("POST", "127.0.0.1:8787",
                                     referer="http://localhost:8787/routes"))

    def test_cross_site_post_rejected(self):
        self.assertFalse(self.allowed("POST", "127.0.0.1:8787", origin="https://evil.example"))
        self.assertFalse(self.allowed("POST", "127.0.0.1:8787", origin="null"))
        self.assertFalse(self.allowed("POST", "127.0.0.1:8787",
                                      referer="https://evil.example/x"))

    def test_non_browser_post(self):
        self.assertTrue(self.allowed("POST", "127.0.0.1:8787"))

    def test_routes_form_keeps_secret(self):
        """路由表单曾只认 webhook/chat_id/token，保存会静默丢掉 secret。"""
        form = {"proj_0": ["p"], "ch_0_0": ["dingtalk-webhook"], "wh_0_0": ["$DT"],
                "sec_0_0": ["$DT_SEC"]}
        doc, err = webui._parse_routes_form(form)
        self.assertEqual(err, "")
        self.assertEqual(doc["routes"]["p"], [{"channel": "dingtalk-webhook", "webhook": "$DT",
                                               "secret": "$DT_SEC"}])
        self.assertIn("$DT_SEC", webui._routes_form(doc).decode("utf-8"))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ZcodeHookTest(unittest.TestCase):
    """端到端：假 rollout → stop-notify.mjs（dry-run）→ feige.py，断言正文完整到达。"""

    def test_dash_body_reaches_feige(self):
        with tempfile.TemporaryDirectory() as d:
            entry = {"type": "model_io", "model": {"modelId": "m1"},
                     "completedAt": "2026-09-26T00:00:00Z",
                     "response": {"finishReason": "stop", "text": "-修复登录"}}
            Path(d, "model-io-sess_x.jsonl").write_text(json.dumps(entry), encoding="utf-8")
            env = {**os.environ, "FEIGE_HOOK_NOTIFY": "1", "FEIGE_DRY_RUN": "1",
                   "ROLLOUT_DIR": d, "PYTHONIOENCODING": "utf-8"}
            r = subprocess.run(["node", str(REPO / "hooks" / "stop-notify.mjs")],
                               input="{}", capture_output=True, env=env,
                               encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("-修复登录", r.stderr)
        self.assertNotIn("expected one argument", r.stderr)


class DebounceTest(unittest.TestCase):
    def test_only_latest_sends(self):
        key = f"test-{os.getpid()}-{time.time_ns()}"
        results = {}
        t1 = threading.Thread(target=lambda: results.update(a=feige.debounce_wait(key, 0.6)))
        t1.start()
        time.sleep(0.2)
        results["b"] = feige.debounce_wait(key, 0.6)
        t1.join()
        self.assertEqual(results, {"a": False, "b": True})

    def test_seconds_from_env(self):
        with mock.patch.dict(os.environ, {"FEIGE_DEBOUNCE_SECONDS": "abc"}):
            self.assertEqual(feige.debounce_seconds(), 0.0)
        with mock.patch.dict(os.environ, {"FEIGE_DEBOUNCE_SECONDS": "90"}):
            self.assertEqual(feige.debounce_seconds(), 90.0)


# ── 端到端：本地假 webhook 服务器 + 真实 hook 进程 ──────────────────────────────

class _Capture(BaseHTTPRequestHandler):
    delay = 0.0
    got: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.delay)
        type(self).got.append((self.path, json.loads(body)))
        data = b'{"code": 0, "errcode": 0}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class E2EBase(unittest.TestCase):
    """每个用例：新的捕获服务器 + 剥干净的环境（不碰真实凭据/路由/日志）。"""

    def setUp(self):
        self.handler = type("H", (_Capture,), {"got": [], "delay": 0.0})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("FEISHU_", "LARK_", "DINGTALK_", "TELEGRAM_", "FEIGE_"))}
        self.env.update({"FEIGE_HOOK_NOTIFY": "1", "PYTHONIOENCODING": "utf-8",
                         "FEIGE_ROUTES_FILE": str(Path(self.tmp.name, "routes.json")),
                         "FEIGE_LOG_FILE": str(Path(self.tmp.name, "feige.log"))})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def wait_for(self, n, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end and len(self.handler.got) < n:
            time.sleep(0.1)
        return self.handler.got

    def transcript(self, text, name="t.jsonl"):
        entry = {"type": "assistant", "timestamp": "2026-09-26T00:00:00Z",
                 "message": {"model": "claude-x", "content": [{"type": "text", "text": text}]}}
        path = Path(self.tmp.name, name)
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def run_claude_hook(self, text, session="s1", name="t.jsonl"):
        payload = {"session_id": session, "cwd": "/x/demo", "transcript_path": self.transcript(text, name)}
        t0 = time.time()
        r = subprocess.run([sys.executable, str(REPO / "adapters" / "claude-code" / "stop_notify.py")],
                           input=json.dumps(payload), capture_output=True, env=self.env,
                           encoding="utf-8", timeout=30)
        return r, time.time() - t0


class ClaudeCodeE2ETest(E2EBase):
    def test_hook_returns_before_network(self):
        """回归：Stop hook 曾同步发送，网络慢多久 Claude Code 就卡多久。"""
        self.handler.delay = 3.0
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        r, took = self.run_claude_hook("-第一行以短横开头")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(took, 2.5, "hook 应在网络返回前就退出")
        got = self.wait_for(1)
        self.assertEqual(len(got), 1, Path(self.env["FEIGE_LOG_FILE"]).read_text(encoding="utf-8")
                         if Path(self.env["FEIGE_LOG_FILE"]).exists() else "no log")
        self.assertIn("-第一行以短横开头", json.dumps(got[0][1], ensure_ascii=False))

    def test_debounce_sends_only_last(self):
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        self.env["FEIGE_DEBOUNCE_SECONDS"] = "1.5"
        self.run_claude_hook("第一轮回复", name="a.jsonl")
        self.run_claude_hook("第二轮回复", name="b.jsonl")
        time.sleep(3.5)
        got = self.wait_for(1)
        self.assertEqual(len(got), 1)
        self.assertIn("第二轮回复", json.dumps(got[0][1], ensure_ascii=False))

    def test_routes_only_sink(self):
        """回归：hook 不走路由；只配了路由（如钉钉）时永远只预览不发。"""
        Path(self.env["FEIGE_ROUTES_FILE"]).write_text(json.dumps(
            {"routes": {"*": [{"channel": "dingtalk-webhook", "webhook": self.url + "/ding"}]}}),
            encoding="utf-8")
        r, _ = self.run_claude_hook("走路由")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.wait_for(1)
        self.assertEqual([p for p, _ in got], ["/ding"])
        self.assertEqual(got[0][1]["msgtype"], "markdown")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ZcodeHookLiveTest(E2EBase):
    def test_routed_detached_send(self):
        Path(self.env["FEIGE_ROUTES_FILE"]).write_text(json.dumps(
            {"routes": {"*": [{"channel": "feishu-webhook", "webhook": self.url + "/zroute"}]}}),
            encoding="utf-8")
        entry = {"type": "model_io", "model": {"modelId": "m1"}, "completedAt": "2026-09-26T00:00:00Z",
                 "response": {"finishReason": "stop", "text": "zcode 战报"}}
        Path(self.tmp.name, "model-io-sess_z.jsonl").write_text(json.dumps(entry), encoding="utf-8")
        self.env["ROLLOUT_DIR"] = self.tmp.name
        r = subprocess.run(["node", str(REPO / "hooks" / "stop-notify.mjs")], input="{}",
                           capture_output=True, env=self.env, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.wait_for(1)
        self.assertEqual([p for p, _ in got], ["/zroute"])
        self.assertIn("zcode 战报", json.dumps(got[0][1], ensure_ascii=False))

    def test_per_turn_stats_and_session_scope(self):
        """mjs 分轮：messages 末条 role==user 的行 = 一轮开始；scope=session 全程累计。"""
        Path(self.env["FEIGE_ROUTES_FILE"]).write_text(json.dumps(
            {"routes": {"*": [{"channel": "feishu-webhook", "webhook": self.url + "/zturn"}]}}),
            encoding="utf-8")

        def entry(sec, last_role, out, reasoning="", tools=0, text=""):
            return {"type": "model_io", "model": {"modelId": "m1"},
                    "completedAt": f"2026-09-26T00:00:{sec:02d}Z",
                    "request": {"body": {"messages": [{"role": last_role, "content": "…"}]}},
                    "response": {"finishReason": "stop", "text": text, "reasoningText": reasoning,
                                 "toolCalls": [{"name": "Bash"}] * tools,
                                 "usage": {"inputTokens": 100, "outputTokens": out}}}

        lines = [entry(0, "user", 100, reasoning="r", tools=1),
                 entry(1, "tool", 20, tools=1),
                 entry(2, "user", 30, reasoning="r", tools=1, text="第二轮战报")]
        Path(self.tmp.name, "model-io-sess_z.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        self.env["ROLLOUT_DIR"] = self.tmp.name

        r = subprocess.run(["node", str(REPO / "hooks" / "stop-notify.mjs")], input="{}",
                           capture_output=True, env=self.env, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.wait_for(1)
        card = json.dumps(got[0][1], ensure_ascii=False)
        self.assertIn("💭1 · 🔧1", card)     # 只算第二轮那一条
        self.assertIn("🎫 30", card)
        self.assertIn("zcode · m1", card)    # 模型脚注带工具名前缀

        self.handler.got.clear()
        env2 = dict(self.env, FEIGE_STATS_SCOPE="session")
        r = subprocess.run(["node", str(REPO / "hooks" / "stop-notify.mjs")], input="{}",
                           capture_output=True, env=env2, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.wait_for(1)
        card = json.dumps(got[0][1], ensure_ascii=False)
        self.assertIn("💭2 · 🔧3", card)     # 全程累计
        self.assertIn("🎫 150", card)


class SummarizeTest(unittest.TestCase):
    """正文 LLM 总结：配置解析、fit_body 优先级、dry-run 零网络、失败兜底。"""

    LONG = "冗长战报" * 5_000  # 远超 BODY_MAX_BYTES

    def _clean_env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_", "DINGTALK_", "TELEGRAM_"))}
        settings = Path(self.tmp.name) / "settings.json"
        env["FEIGE_SETTINGS_FILE"] = str(settings)
        env.update(extra)
        return env, settings

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_disabled_without_key(self):
        env, _ = self._clean_env()
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(feige.llm_config()["enabled"])
            with mock.patch.object(feige, "llm_summarize") as ls:
                out = feige.fit_body(self.LONG)
        ls.assert_not_called()  # 未启用连总结器都不进
        self.assertIn("内容过长已截断", out)

    def test_enabled_with_env_key(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test-123")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = feige.llm_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["model"], feige.DEFAULT_LLM_MODEL)
        self.assertEqual(cfg["base_url"], feige.DEFAULT_LLM_BASE_URL)

    def test_settings_file_config_and_explicit_off(self):
        env, settings = self._clean_env()
        settings.write_text(json.dumps({"llm_api_key": "sk-from-settings",
                                        "llm_model": "glm-4-flash",
                                        "llm_max_chars": "200"}), encoding="utf-8")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = feige.llm_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual((cfg["model"], cfg["max_chars"]), ("glm-4-flash", 200))
        # 显式关闭：summarize=false 即使有 key 也不启用（False 不能被当「未配置」）
        settings.write_text(json.dumps({"llm_api_key": "sk-x", "summarize": False}),
                            encoding="utf-8")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(feige.llm_config()["enabled"])

    def test_fit_body_uses_summary_when_over_limit(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize", return_value="浓缩版战报") as ls:
            out = feige.fit_body(self.LONG)
        ls.assert_called_once()
        self.assertEqual(out, "浓缩版战报")

    def test_fit_body_falls_back_when_summarizer_fails(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize", return_value=""):
            out = feige.fit_body(self.LONG)
        self.assertIn("内容过长已截断", out)

    def test_short_body_never_hits_summarizer(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize") as ls:
            self.assertEqual(feige.fit_body("短正文"), "短正文")
        ls.assert_not_called()

    def test_dry_run_never_calls_llm(self):
        """回归：dry-run/预览在 hook 前台同步执行，绝不能在这里发起 LLM 网络调用。"""
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test")
        for channel in ("feishu-webhook", "dingtalk-webhook", "telegram"):
            with self.subTest(channel=channel), \
                    mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(feige, "llm_summarize") as ls:
                ok, out = feige.send_report("t", self.LONG, channel=channel, dry_run=True)
                self.assertTrue(ok)
                ls.assert_not_called()
                self.assertIn("内容过长已截断", out)

    def test_real_send_uses_llm(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test",
                                 FEISHU_CARD_WEBHOOK="https://fake.invalid/hook/x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize", return_value="摘要版"), \
                mock.patch.object(feige, "_post_json", return_value={"code": 0}) as post:
            ok, detail = feige.send_report("t", self.LONG, channel="feishu-webhook")
        self.assertTrue(ok, detail)
        sent = json.dumps(post.call_args[0][1], ensure_ascii=False)
        self.assertIn("摘要版", sent)
        self.assertNotIn("内容过长已截断", sent)

    def test_llm_summarize_request_shape_and_failure_swallowed(self):
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(
                {"choices": [{"message": {"content": " 摘要结果 "}}]}).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()

        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-abcd1234",
                                 FEIGE_LLM_BASE_URL="https://gw.local/v1/",
                                 FEIGE_LLM_MODEL="m-x", FEIGE_LLM_MAX_CHARS="120")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(feige.llm_summarize("长" * 30_000), "摘要结果")
        self.assertEqual(captured["url"], "https://gw.local/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer sk-abcd1234")
        self.assertEqual(captured["body"]["model"], "m-x")
        self.assertLessEqual(len(captured["body"]["messages"][1]["content"]),
                             feige.LLM_INPUT_MAX_CHARS)
        # 网络炸了 → 空串 + 打码日志，绝不抛
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen",
                           side_effect=urllib.error.URLError("refused")):
            self.assertEqual(feige.llm_summarize("x"), "")

    def test_redact_covers_llm_key(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-supersecret01")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn("sk-supersecret01", feige.redact("boom sk-supersecret01"))

    def test_fetch_models_parses_response_and_sorts(self):
        fake_json = json.dumps({
            "data": [
                {"id": "openai/gpt-4o", "owned_by": "openai", "context_window": "128000"},
                {"id": "anthropic/claude-3-5-sonnet", "owned_by": "anthropic", "context_length": 200000},
                {"id": "deepseek/deepseek-chat", "owned_by": "deepseek"}
            ]
        }).encode("utf-8")

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return fake_json

        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            res = feige.fetch_models(base_url="https://api.openai.com/v1", api_key="sk-test")
            self.assertEqual(len(res), 3)
            self.assertEqual(res[0]["id"], "anthropic/claude-3-5-sonnet")  # 字母顺序排序
            self.assertEqual(res[0]["context_window"], 200000)
            self.assertEqual(res[1]["id"], "deepseek/deepseek-chat")
            self.assertIsNone(res[1]["context_window"])
            self.assertEqual(res[2]["id"], "openai/gpt-4o")
            self.assertEqual(res[2]["context_window"], 128000)

    def test_fetch_models_error_handling(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(RuntimeError) as cm:
                feige.fetch_models(base_url="http://127.0.0.1:9999/v1", api_key="sk-test")
            self.assertIn("无法连接模型服务", str(cm.exception))


class AdapterBodyTest(unittest.TestCase):
    """适配器正文口径：总结未启用 teaser 300 字；启用时放行更长原文给总结器。"""

    def setUp(self):
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        self.common = common
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FEIGE_")}
        env["FEIGE_SETTINGS_FILE"] = str(Path(self.tmp.name) / "s.json")
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_teaser_when_summarize_off(self):
        out = self.common.body_for_card("长" * 1000)
        self.assertEqual(out, "长" * 300 + " ……")

    def test_full_body_when_summarize_on(self):
        os.environ["FEIGE_LLM_API_KEY"] = "sk-test"
        out = self.common.body_for_card("长" * 1000)
        self.assertEqual(out, "长" * 1000)  # ≤6000 字原样放行
        big = self.common.body_for_card("长" * 9000)
        self.assertEqual(len(big), self.common.FULL_BODY_CHARS + 3)  # 截到上限带省略号

    def test_empty_text_keeps_placeholder(self):
        self.assertEqual(self.common.body_for_card("   "), "……（本轮无文本输出）")


class ClipTest(unittest.TestCase):
    LONG = "战报正文" * 10_000  # 40k 汉字 ≈ 120KB

    def setUp(self):
        # 本机真配了总结 LLM 也不能让截断用例走网络：清空 key + 设置文件指向不存在路径
        env = {k: v for k, v in os.environ.items()
               if k not in ("FEIGE_LLM_API_KEY", "FEIGE_SUMMARIZE")}
        env["FEIGE_SETTINGS_FILE"] = str(Path(tempfile.gettempdir()) / "feige-no-such-settings.json")
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_short_untouched(self):
        self.assertEqual(feige.clip("短"), "短")

    def test_feishu_payload_under_webhook_limit(self):
        _, out = feige.send_report("t", self.LONG, channel="feishu-webhook", dry_run=True)
        payload = json.dumps(json.loads(out), ensure_ascii=False).encode("utf-8")
        self.assertLess(len(payload), 20 * 1024)
        self.assertIn("内容过长已截断", out)

    def test_dingtalk_payload_under_limit(self):
        _, out = feige.send_report("t", self.LONG, channel="dingtalk-webhook", dry_run=True)
        payload = json.loads(out)["payload"]
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), 20_000)

    def test_telegram_utf16_limit_with_emoji(self):
        text = feige.render_telegram_text("🕊️" * 10, "😀" * 5000,
                                          stats={"project": "p", "tools": 3}, status="ok")
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertTrue(text.endswith("📦 p · 🔧3"), "截的是正文，脚注要保住")


class CardkitTest(unittest.TestCase):
    def test_two_calls_final_card(self):
        """回归：旧流程建流式卡→更新同样正文→整卡 PUT→PATCH 关流式→发群，5 次调用。"""
        calls = []

        def fake(method, path, body=None):
            calls.append((method, path.split("?")[0], body))
            return {"code": 0, "data": {"card_id": "c1", "message_id": "m1"}}

        with mock.patch.object(feige, "call_app_api", side_effect=fake):
            ok, detail = feige.send_report("t", "b", stats={"tools": 2}, channel="feishu-cardkit",
                                           chat_id="oc_x")
        self.assertTrue(ok, detail)
        self.assertEqual([(m, p) for m, p, _ in calls],
                         [("POST", "/cardkit/v1/cards"), ("POST", "/im/v1/messages")])
        card = json.loads(calls[0][2]["data"])
        self.assertNotIn("streaming_mode", card["config"])
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("🔧2", json.dumps(card, ensure_ascii=False))


class RedactUrlTest(unittest.TestCase):
    def test_route_level_webhooks(self):
        s = feige.redact("POST https://open.feishu.cn/open-apis/bot/v2/hook/abcdef-123456 failed; "
                         "https://oapi.dingtalk.com/robot/send?access_token=deadbeef99&timestamp=1&sign=xyz%2B")
        for leaked in ("abcdef-123456", "deadbeef99", "xyz%2B"):
            self.assertNotIn(leaked, s)
        self.assertIn("design=ok", feige.redact("design=ok"))


class SignE2ETest(E2EBase):
    """签名/加签：本地假 webhook 收到的请求按官方算法独立复核。"""

    def send(self, **kw):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return feige.send_report("t", "b", **kw)

    def test_feishu_signed(self):
        self.env.update(FEISHU_CARD_WEBHOOK=self.url + "/hook", FEISHU_WEBHOOK_SECRET="s3cret")
        ok, detail = self.send(channel="feishu-webhook")
        self.assertTrue(ok, detail)
        body = self.handler.got[0][1]
        key = f"{body['timestamp']}\ns3cret".encode()
        expect = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
        self.assertEqual(body["sign"], expect)
        self.assertLess(abs(int(body["timestamp"]) - time.time()), 60)

    def test_env_secret_not_used_for_route_webhook(self):
        self.env["FEISHU_WEBHOOK_SECRET"] = "for-the-default-bot"
        ok, _ = self.send(channel="feishu-webhook", webhook=self.url + "/other")
        self.assertTrue(ok)
        self.assertNotIn("sign", self.handler.got[0][1])

    def test_dingtalk_signed(self):
        self.env.update(DINGTALK_WEBHOOK=self.url + "/ding?access_token=t0k", DINGTALK_SECRET="SECabc")
        ok, detail = self.send(channel="dingtalk-webhook")
        self.assertTrue(ok, detail)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.handler.got[0][0]).query)
        ts = q["timestamp"][0]
        expect = base64.b64encode(hmac.new(b"SECabc", f"{ts}\nSECabc".encode(),
                                           hashlib.sha256).digest()).decode()
        self.assertEqual(q["sign"][0], expect)  # parse_qs 已做 URL 解码
        self.assertEqual(q["access_token"][0], "t0k")

    def test_route_secret_passed_through(self):
        doc = {"routes": {"p": [{"channel": "dingtalk-webhook", "webhook": self.url + "/ding",
                                 "secret": "$ROUTE_SEC"}]}}
        self.env["ROUTE_SEC"] = "SECroute"
        with mock.patch.dict(os.environ, self.env, clear=True):
            ok, detail = feige.send_routed("t", "b", project="p", doc=doc)
        self.assertTrue(ok, detail)
        self.assertIn("sign=", self.handler.got[0][0])


# ── 插件：统一 Stop hook 分发 + 插件设置映射 + Codex notify 包装链 ─────────────

def _load_module(path: Path, name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DetectHostTest(unittest.TestCase):
    stop = _load_module(REPO / "hooks" / "stop.py", "feige_stop_hook")

    def test_hosts(self):
        d = self.stop.detect_host
        self.assertEqual(d({}, env={"ZCODE_SESSION_ID": "z"}), "zcode")
        self.assertEqual(d({"turn_id": "t", "session_id": "s"}, env={}), "codex")
        self.assertEqual(d({"transcript_path": r"C:\Users\u\.codex\sessions\r.jsonl"}, env={}), "codex")
        self.assertEqual(d({"transcript_path": "/home/u/.claude/projects/x.jsonl"}, env={}), "claude-code")


class ClaudeTranscriptTest(unittest.TestCase):
    def test_context_counts_cache_creation(self):
        """真机实测发现：首轮几乎全是 cache_creation，漏算时上下文显示成个位数。"""
        mod = _load_module(REPO / "adapters" / "claude-code" / "stop_notify.py", "feige_cc_t")
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "FEIGE_CONTEXT_TOTAL")}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env, clear=True):
            entry = {"type": "assistant", "timestamp": "2026-09-26T00:00:00Z",
                     "message": {"model": "m", "content": [{"type": "text", "text": "hi"}],
                                 "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20000,
                                           "cache_read_input_tokens": 5000}}}
            path = Path(d, "t.jsonl")
            path.write_text(json.dumps(entry), encoding="utf-8")
            self.assertEqual(mod.summarize_transcript(str(path))["context"], "25.0k")


class AdaptersPageTest(unittest.TestCase):
    def test_detects_plugin_installs(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for rel in (".zcode/cli/plugins/installed_plugins.json", ".claude/plugins/installed_plugins.json"):
                (home / rel).parent.mkdir(parents=True)
                (home / rel).write_text('{"plugins": {"feige-fry-cards@feige-fry-cards": {}}}', encoding="utf-8")
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                'notify = ["x.exe"]\n[plugins."feige-fry-cards@feige-fry-cards"]\nenabled = true\n',
                encoding="utf-8")
            (home / ".kimi-code").mkdir()
            (home / ".kimi-code" / "config.toml").write_text(
                '[[hooks]]\nevent = "Stop"\ncommand = \'python "C:/repo/adapters/kimi-code/stop_notify.py"\'\n',
                encoding="utf-8")
            html_ = webui.page_adapters(home).decode("utf-8")
        self.assertEqual(html_.count("已安装 feige-fry-cards 插件"), 3)
        self.assertIn("已手挂 Stop hook", html_)
        self.assertNotIn("未接入", html_)


class PluginOptionsTest(unittest.TestCase):
    def test_mapping_real_env_wins(self):
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        env = {"CLAUDE_PLUGIN_OPTION_HOOK_NOTIFY": "true",
               "CLAUDE_PLUGIN_OPTION_WEBHOOK_URL": "https://plugin/hook",
               "ZCODE_USER_CONFIG_APP_ID": "cli_z",
               "FEISHU_NOTIFY_CHAT_ID": "oc_real", "CLAUDE_PLUGIN_OPTION_NOTIFY_CHAT_ID": "oc_plugin"}
        common.apply_plugin_options(env)
        self.assertEqual(env["FEIGE_HOOK_NOTIFY"], "true")
        self.assertEqual(env["FEISHU_CARD_WEBHOOK"], "https://plugin/hook")
        self.assertEqual(env["FEISHU_APP_ID"], "cli_z")
        self.assertEqual(env["FEISHU_NOTIFY_CHAT_ID"], "oc_real")


class PluginHookE2ETest(E2EBase):
    """hooks/stop.py 真进程：各宿主 payload → 本地假 webhook 收到卡；stdout 必须为空。"""

    def run_stop(self, payload, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        return subprocess.run([sys.executable, str(REPO / "hooks" / "stop.py")],
                              input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                              env=env, encoding="utf-8", timeout=60)

    def test_claude_code_via_plugin_options(self):
        """只填插件设置（CLAUDE_PLUGIN_OPTION_*），不设任何 FEIGE_/FEISHU_ 环境变量。"""
        del self.env["FEIGE_HOOK_NOTIFY"]
        r = self.run_stop({"session_id": "s", "cwd": "/x/demo", "hook_event_name": "Stop",
                           "transcript_path": self.transcript("claude 插件战报")},
                          {"CLAUDE_PLUGIN_OPTION_HOOK_NOTIFY": "true",
                           "CLAUDE_PLUGIN_OPTION_WEBHOOK_URL": self.url + "/hook"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        got = self.wait_for(1)
        self.assertIn("claude 插件战报", json.dumps(got[0][1], ensure_ascii=False))
        self.assertIn("Claude Code", json.dumps(got[0][1], ensure_ascii=False))

    def test_codex_plugin_hook(self):
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        r = self.run_stop({"session_id": "s", "turn_id": "t1", "cwd": "/x/demo", "model": "gpt-9",
                           "hook_event_name": "Stop", "stop_hook_active": False,
                           "transcript_path": None, "last_assistant_message": "codex 插件战报"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        card = json.dumps(self.wait_for(1)[0][1], ensure_ascii=False)
        self.assertIn("codex 插件战报", card)
        self.assertIn("gpt-9", card)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_zcode_dispatch(self):
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        entry = {"type": "model_io", "model": {"modelId": "glm-x"}, "completedAt": "2026-09-26T00:00:00Z",
                 "response": {"finishReason": "stop", "text": "zcode 插件战报"}}
        Path(self.tmp.name, "model-io-sess_q.jsonl").write_text(json.dumps(entry, ensure_ascii=False),
                                                                encoding="utf-8")
        r = self.run_stop({"session_id": "q"}, {"ZCODE_SESSION_ID": "q", "ROLLOUT_DIR": self.tmp.name})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertIn("zcode 插件战报", json.dumps(self.wait_for(1)[0][1], ensure_ascii=False))

    def test_disabled_is_silent(self):
        del self.env["FEIGE_HOOK_NOTIFY"]
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        r = self.run_stop({"turn_id": "t", "last_assistant_message": "x"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        time.sleep(1.5)
        self.assertEqual(self.handler.got, [])


class CodexNotifyChainTest(E2EBase):
    def test_chained_notify_runs_and_exit_code_passes_through(self):
        """包装链：原 notify 收到「原参数 + 事件 JSON」，飞鸽照发卡，退出码透传。"""
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        marker = Path(self.tmp.name, "chained.json")
        chained = [sys.executable, "-c",
                   "import sys,json,pathlib;pathlib.Path(sys.argv[1]).write_text("
                   "json.dumps(sys.argv[2:]),encoding='utf-8');sys.exit(3)", str(marker), "turn-ended"]
        event = json.dumps({"type": "agent-turn-complete", "thread-id": "th", "cwd": "/x/demo",
                            "last-assistant-message": "链式 notify"}, ensure_ascii=False)
        r = subprocess.run([sys.executable, str(REPO / "adapters" / "codex" / "notify.py"), *chained, event],
                           capture_output=True, env=self.env, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), ["turn-ended", event])
        self.assertIn("链式 notify", json.dumps(self.wait_for(1)[0][1], ensure_ascii=False))

    def test_chain_runs_even_when_feige_disabled(self):
        del self.env["FEIGE_HOOK_NOTIFY"]
        marker = Path(self.tmp.name, "ran")
        chained = [sys.executable, "-c", "import sys,pathlib;pathlib.Path(sys.argv[1]).touch()", str(marker)]
        r = subprocess.run([sys.executable, str(REPO / "adapters" / "codex" / "notify.py"), *chained,
                            '{"type": "agent-turn-complete"}'],
                           capture_output=True, env=self.env, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(marker.exists())


class ClaudePerTurnTest(unittest.TestCase):
    """Claude 适配器单轮口径：以最后一条真实用户提问分轮；无提问行退化全文件。"""

    @staticmethod
    def _load():
        spec = importlib.util.spec_from_file_location(
            "feige_claude_ut", REPO / "adapters" / "claude-code" / "stop_notify.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @classmethod
    def setUpClass(cls):
        cls.claude = cls._load()

    def write(self, tmp, lines):
        p = Path(tmp, "t.jsonl")
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                     encoding="utf-8")
        return str(p)

    def asst(self, ts, text="", thinking=False, tools=0, out=0):
        parts = ([{"type": "thinking", "thinking": "…"}] if thinking else []) \
            + [{"type": "tool_use", "name": "Bash"}] * tools \
            + ([{"type": "text", "text": text}] if text else [])
        return {"type": "assistant", "timestamp": f"2026-09-26T00:00:{ts:02d}Z",
                "message": {"model": "claude-x", "content": parts,
                            "usage": {"input_tokens": 10, "output_tokens": out}}}

    def test_per_turn_stats(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, [
                {"type": "user", "timestamp": "2026-09-26T00:00:00Z",
                 "message": {"content": "任务一"}},
                self.asst(1, thinking=True, tools=2, out=100),
                self.asst(2, text="第一轮", out=50),
                {"type": "user", "timestamp": "2026-09-26T00:00:03Z",
                 "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                {"type": "user", "timestamp": "2026-09-26T00:00:04Z",
                 "message": {"content": [{"type": "text", "text": "任务二"}]}},
                self.asst(5, thinking=True, text="第二轮", out=30),
            ])
            s = self.claude.summarize_transcript(path)
            # 只算「任务二」本轮；tool_result 行不算提问、不重置
            self.assertEqual((s["thinking"], s["tools"], s["tokens"]), (1, 0, 30))
            self.assertEqual(s["elapsed"], "1.0s")
            self.assertEqual(s["body"], "第二轮")
            self.assertEqual(s["model"], "claude-code · claude-x")

    def test_session_scope_uses_whole_file(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FEIGE_STATS_SCOPE": "session"}):
            path = self.write(d, [
                {"type": "user", "timestamp": "2026-09-26T00:00:00Z", "message": {"content": "任务一"}},
                self.asst(1, thinking=True, tools=2, out=100),
                self.asst(2, text="第二轮", out=50),
            ])
            s = self.claude.summarize_transcript(path)
        self.assertEqual((s["thinking"], s["tools"], s["tokens"]), (1, 2, 150))

    def test_no_user_prompt_falls_back_to_whole_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, [
                self.asst(0, thinking=True, tools=1, out=10),
                self.asst(5, text="只有回复", out=5),
            ])
            s = self.claude.summarize_transcript(path)
            self.assertEqual((s["thinking"], s["tools"], s["tokens"]), (1, 1, 15))
            self.assertEqual(s["elapsed"], "5.0s")

    def test_compact_summary_does_not_break_turn(self):
        """转录压缩摘要行 (isCompactSummary) 不算真实用户提问，不应重置分轮。"""
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, [
                {"type": "user", "timestamp": "2026-09-26T00:00:00Z",
                 "message": {"content": "真实提问"}},
                self.asst(1, thinking=True, tools=2, out=50),
                {"type": "user", "timestamp": "2026-09-26T00:00:02Z",
                 "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
                 "message": {"content": "This session is being continued..."}},
                self.asst(3, thinking=True, tools=1, out=30),
            ])
            s = self.claude.summarize_transcript(path)
            # 真实提问后跨过 compact summary 累计为 2 思考、3 工具、80 token
            self.assertEqual((s["thinking"], s["tools"], s["tokens"]), (2, 3, 80))
            self.assertEqual(s["elapsed"], "3.0s")

    def test_context_window_and_formatting(self):
        """上下文格式化：已知模型带分母与百分比，未知模型退化为纯水位。"""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "FEIGE_CONTEXT_TOTAL")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.claude.context_window("gemini-3.8-flash"), 1_048_576)
            self.assertEqual(self.claude.context_window("claude-3-7-sonnet"), 200_000)
            self.assertEqual(self.claude.context_window("claude-fable-5-1"), 1_048_576)
            self.assertEqual(self.claude.context_window("unknown-model-xyz"), 0)

        # fmt_context
        self.assertEqual(self.claude.fmt_context(218_828, 1_048_576), "218.8k/1.0m (21%)")
        self.assertEqual(self.claude.fmt_context(218_828, 0), "218.8k")
        self.assertEqual(self.claude.fmt_context(0, 1_048_576), "")


if __name__ == "__main__":
    unittest.main()
