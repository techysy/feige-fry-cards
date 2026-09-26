"""feige 回归测试（纯标准库 unittest，不发网络）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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


class ClipTest(unittest.TestCase):
    LONG = "战报正文" * 10_000  # 40k 汉字 ≈ 120KB

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


if __name__ == "__main__":
    unittest.main()
