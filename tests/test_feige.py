"""feige 回归测试（纯标准库 unittest，不发网络）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
