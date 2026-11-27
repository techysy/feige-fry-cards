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
    def setUp(self):
        # 脚注字段顺序默认来自设置文件，测试必须与本机 ~/.feige-settings.json
        # 及环境变量隔离，否则用户改过 footer_fields 时断言会随机器漂移
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SETTINGS_FILE"] = str(Path(self.tmp.name) / "absent.json")
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

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


class PanelFooterSplitTest(unittest.TestCase):
    """面板头 / 脚注独立配置 + 完成卡信息去重（标题已含项目与宿主，统计只出一遍）。"""

    STATS = {"project": "CreditDaddy", "model": "claude-code · qwen4-max",
             "thinking": 2, "tools": 45, "context": "44.4k/1.0m (4%)",
             "tokens": 1700, "elapsed": "5m 34s"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "s.json"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SETTINGS_FILE"] = str(self.settings)
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _elements(self, **kw):
        return feige.build_card("CreditDaddy · Claude Code 任务完成", "摘要",
                                stats=dict(self.STATS), status="ok", **kw)["body"]["elements"]

    def test_completed_card_has_no_footer_by_default(self):
        els = self._elements()
        self.assertFalse(any(e.get("tag") == "hr" for e in els))
        self.assertEqual([e for e in els if e.get("text_size") == "notation"], [])
        self.assertTrue(any(e.get("tag") == "collapsible_panel" for e in els))

    def test_panel_header_omits_project_by_default(self):
        header = feige._build_panel_header_text(self.STATS)
        self.assertNotIn("CreditDaddy", header)   # 标题已有项目名，面板头不重复
        self.assertNotIn("📦", header)
        self.assertIn("💭2", header)
        self.assertIn("🔧45", header)
        self.assertIn("⏱️ 5m 34s", header)

    def test_footer_switch_restores_hr_and_notation(self):
        os.environ["FEIGE_FOOTER_ENABLED"] = "1"
        els = self._elements()
        self.assertTrue(any(e.get("tag") == "hr" for e in els))
        foot = [e for e in els if e.get("text_size") == "notation"]
        self.assertEqual(len(foot), 1)
        self.assertIn("📦 CreditDaddy", foot[0]["content"])  # 脚注仍走 footer_fields 全字段

    def test_panel_and_footer_fields_are_independent(self):
        self.settings.write_text(json.dumps({
            "panel_fields": ["tools", "elapsed"],
            "footer_fields": ["project", "model"],
        }), encoding="utf-8")
        self.assertEqual(feige._panel_field_order(), ("tools", "elapsed"))
        self.assertEqual(feige._footer_field_order(), ("project", "model"))
        header = feige._build_panel_header_text(self.STATS)
        self.assertEqual(header, "🔧45 · ⏱️ 5m 34s")
        self.assertEqual(feige.stats_footer(self.STATS), "📦 CreditDaddy · claude-code · qwen4-max")

    def test_legacy_footer_fields_does_not_migrate_to_panel(self):
        """旧配置只有 footer_fields：面板走新默认（无项目），脚注保持旧口径。"""
        self.settings.write_text(json.dumps({
            "footer_fields": ["project", "model", "thinking", "tools",
                              "context", "tokens", "elapsed"],
        }), encoding="utf-8")
        self.assertNotIn("project", feige._panel_field_order())
        self.assertIn("project", feige._footer_field_order())

    def test_panel_fields_env_overrides_settings(self):
        self.settings.write_text(json.dumps({"panel_fields": ["tools"]}), encoding="utf-8")
        os.environ["FEIGE_PANEL_FIELDS"] = "model,elapsed"
        self.assertEqual(feige._panel_field_order(), ("model", "elapsed"))

    def test_empty_panel_fields_hide_all_stats(self):
        self.settings.write_text(json.dumps({"panel_fields": []}), encoding="utf-8")
        self.assertEqual(feige._build_panel_header_text(self.STATS), "会话详情与统计")

    def test_footer_enabled_reads_settings_file(self):
        self.assertFalse(feige.footer_enabled())
        self.settings.write_text(json.dumps({"footer_enabled": "1"}), encoding="utf-8")
        self.assertTrue(feige.footer_enabled())
        self.settings.write_text(json.dumps({"footer_enabled": "0"}), encoding="utf-8")
        self.assertFalse(feige.footer_enabled())

    def test_static_channels_keep_footer_regardless(self):
        """钉钉/Telegram 静态版式没有面板，脚注照旧输出，不受 footer_enabled 影响。"""
        ding = feige.render_dingtalk("t", "b", stats=dict(self.STATS),
                                     allow_summarize=False)
        self.assertIn("📦 CreditDaddy", ding["markdown"]["text"])
        tg = feige.render_telegram_text("t", "b", stats=dict(self.STATS),
                                        allow_summarize=False)
        self.assertIn("📦 CreditDaddy", tg)


class ContextStyleTest(unittest.TestCase):
    """面板头上下文样式三档（text / bar / text_bar，默认 text_bar），只作用面板头。"""

    STATS = {"model": "m", "context": "44.4k/1.0m (4%)", "tokens": 1700}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "s.json"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SETTINGS_FILE"] = str(self.settings)
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _panel_header(self, **kw):
        card = feige.build_card("t", "b", stats=dict(self.STATS), status="ok", **kw)
        panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        return panels[0]["header"]["title"]["content"]

    def test_default_is_text_bar(self):
        self.assertEqual(feige.context_style(), "text_bar")
        self.assertIn("44.4k/1.0m [▓░░░░░░░] 4%", self._panel_header())

    def test_three_styles_render(self):
        """三档口径对齐 claw / hermes-fry-cards：text 纯文本、bar 纯条、text_bar 组合。"""
        cases = {
            "text": "44.4k/1.0m (4%)",
            "bar": "[▓░░░░░░░] 4%",
            "text_bar": "44.4k/1.0m [▓░░░░░░░] 4%",
        }
        for style, expected in cases.items():
            self.settings.write_text(json.dumps({"context_style": style}), encoding="utf-8")
            self.assertEqual(feige.context_style(), style)
            self.assertIn(expected, self._panel_header())
            self.assertEqual(feige._format_panel_context("44.4k/1.0m (4%)", style), expected)

    def test_legacy_plain_maps_to_text(self):
        """2.0.x 试用的 plain 等价家族 text；设置与环境变量两层都归一。"""
        self.settings.write_text(json.dumps({"context_style": "plain"}), encoding="utf-8")
        self.assertEqual(feige.context_style(), "text")
        self.assertIn("44.4k/1.0m (4%)", self._panel_header())
        os.environ["FEIGE_CONTEXT_STYLE"] = "plain"
        self.assertEqual(feige.context_style(), "text")
        del os.environ["FEIGE_CONTEXT_STYLE"]

    def test_env_overrides_settings(self):
        self.settings.write_text(json.dumps({"context_style": "text"}), encoding="utf-8")
        os.environ["FEIGE_CONTEXT_STYLE"] = "bar"
        self.assertEqual(feige.context_style(), "bar")
        self.assertIn("[▓░░░░░░░] 4%", self._panel_header())

    def test_bogus_value_falls_back(self):
        self.settings.write_text(json.dumps({"context_style": "sparkline"}), encoding="utf-8")
        self.assertEqual(feige.context_style(), "text_bar")
        self.assertIn("[▓░░░░░░░] 4%", self._panel_header())

    def test_explicit_override_argument(self):
        """显式 context_style 参数（Studio 预览用）优先于设置，不改任何配置。"""
        self.settings.write_text(json.dumps({"context_style": "text_bar"}), encoding="utf-8")
        header = self._panel_header(context_style="text")
        self.assertIn("44.4k/1.0m (4%)", header)

    def test_bar_drops_text_segment(self):
        """bar 档只留进度条：有文本时丢弃 '44.4k/1.0m' 前缀，百分比保留。"""
        self.settings.write_text(json.dumps({"context_style": "bar"}), encoding="utf-8")
        header = self._panel_header()
        self.assertIn("[▓░░░░░░░] 4%", header)
        self.assertNotIn("44.4k/1.0m", header)

    def test_footer_and_static_channels_unaffected(self):
        """脚注与静态渠道保持原样口径，不受 context_style 影响。"""
        for style in ("text", "bar", "text_bar"):
            self.settings.write_text(json.dumps({"context_style": style}), encoding="utf-8")
            self.assertEqual(feige.stats_footer(self.STATS), "m · 44.4k/1.0m (4%) · 🎫 1.7k")
            ding = feige.render_dingtalk("t", "b", stats=dict(self.STATS), allow_summarize=False)
            self.assertIn("44.4k/1.0m (4%)", ding["markdown"]["text"])

    def test_percent_only_string(self):
        self.assertEqual(feige._format_panel_context("42%", "text"), "42%")
        self.assertEqual(feige._format_panel_context("42%", "bar"), "[███▓░░░░] 42%")
        self.assertEqual(feige._format_panel_context("42%", "text_bar"), "[███▓░░░░] 42%")
        self.settings.write_text(json.dumps({"context_style": "text"}), encoding="utf-8")
        self.assertEqual(feige._format_panel_context("42%"), "42%")


class PanelHostPrefixTest(unittest.TestCase):
    """面板头模型名默认不带宿主前缀（卡片标题已含「项目 · 宿主」，不再重复）。"""

    STATS = {"model": "claude-code · qwen4-max", "context": "44.4k/1.0m (4%)",
             "thinking": 2, "tools": 45, "tokens": 1700, "elapsed": "5m 34s"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "s.json"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SETTINGS_FILE"] = str(self.settings)
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _header(self, **kw):
        card = feige.build_card("t", "b", stats=dict(self.STATS), status="ok", **kw)
        panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        return panels[0]["header"]["title"]["content"]

    def test_default_strips_host_prefix(self):
        self.assertFalse(feige.panel_host_prefix_enabled())
        self.assertEqual(feige.format_panel_model("claude-code · qwen4-max"), "Qwen 4-max")
        header = self._header()
        self.assertIn("Qwen 4-max", header)
        self.assertNotIn("claude-code", header)
        # 其余字段照旧
        self.assertIn("💭2", header)
        self.assertIn("🔧45", header)
        self.assertIn("⏱️ 5m 34s", header)

    def test_settings_or_env_can_restore_prefix(self):
        self.settings.write_text(json.dumps({"panel_host_prefix": "1"}), encoding="utf-8")
        self.assertTrue(feige.panel_host_prefix_enabled())
        self.assertEqual(feige.format_panel_model("claude-code · qwen4-max"),
                         "claude-code · Qwen 4-max")
        self.assertIn("claude-code · Qwen 4-max", self._header())
        os.environ["FEIGE_PANEL_HOST_PREFIX"] = "0"   # 环境变量优先
        self.assertFalse(feige.panel_host_prefix_enabled())
        self.assertNotIn("claude-code", self._header())

    def test_explicit_argument_overrides_settings(self):
        header = self._header(panel_host_prefix=True)
        self.assertIn("claude-code · Qwen 4-max", header)
        self.settings.write_text(json.dumps({"panel_host_prefix": "1"}), encoding="utf-8")
        self.assertNotIn("claude-code", self._header(panel_host_prefix=False))

    def test_unprefixed_and_unknown_models(self):
        self.assertEqual(feige.format_panel_model("tencent/hy4-flash:free"), "Hy4 flash")
        self.assertEqual(feige.format_panel_model("deepseek-v4.1-flash"), "DeepSeek v4.1-flash")
        self.assertEqual(feige.format_panel_model("unknown/custom-model"), "custom-model")
        self.assertEqual(feige.format_panel_model(""), "")

    def test_footer_and_static_channels_keep_raw_model(self):
        """脚注与钉钉/Telegram 是另一条渲染路径，不受此开关影响。"""
        self.assertIn("claude-code · qwen4-max", feige.stats_footer(self.STATS))
        ding = feige.render_dingtalk("t", "b", stats=dict(self.STATS), allow_summarize=False)
        self.assertIn("claude-code · qwen4-max", ding["markdown"]["text"])


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

    def test_target_without_chat_id_uses_project_chat(self):
        """每群自动：目标只写 channel 时，群应落到「本项目」的 chat_id，而非全局默认群。"""
        doc = {"routes": {"proj": [{"channel": "feishu-cardkit"}]}}
        with mock.patch.dict(os.environ, {"FEISHU_CHAT_ID__proj": "oc_project",
                                          "FEISHU_NOTIFY_CHAT_ID": "oc_default"}, clear=True), \
             mock.patch.object(feige, "send_report", return_value=(True, "ok")) as sr:
            feige.send_routed("t", "b", project="proj", doc=doc)
        self.assertEqual(sr.call_args.kwargs["chat_id"], "oc_project")

    def test_target_chat_id_wins_over_project_chat(self):
        doc = {"routes": {"proj": [{"channel": "feishu-cardkit", "chat_id": "oc_explicit"}]}}
        with mock.patch.dict(os.environ, {"FEISHU_CHAT_ID__proj": "oc_project"}, clear=True), \
             mock.patch.object(feige, "send_report", return_value=(True, "ok")) as sr:
            feige.send_routed("t", "b", project="proj", doc=doc)
        self.assertEqual(sr.call_args.kwargs["chat_id"], "oc_explicit")

    def test_project_chat_falls_back_to_default(self):
        doc = {"routes": {"proj": [{"channel": "feishu-cardkit"}]}}
        with mock.patch.dict(os.environ, {"FEISHU_NOTIFY_CHAT_ID": "oc_default"}, clear=True), \
             mock.patch.object(feige, "load_settings", return_value={}), \
             mock.patch.object(feige, "send_report", return_value=(True, "ok")) as sr:
            feige.send_routed("t", "b", project="proj", doc=doc)
        self.assertEqual(sr.call_args.kwargs["chat_id"], "oc_default")

    def test_project_chat_from_settings_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "s.json")
            path.write_text(json.dumps({"feishu_chat_id__proj": "oc_from_file"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"FEIGE_SETTINGS_FILE": str(path)}, clear=True):
                self.assertEqual(feige.project_chat_id("proj"), "oc_from_file")
                self.assertEqual(feige.project_chat_id(""), "")  # 无全局默认 → 空


class AutoNotifyTest(unittest.TestCase):
    """「每个群自动开启」：路由里配了项目 → 未开全局 hook 也自动发。"""

    def test_routes_auto_enabled_by_project(self):
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        doc = {"routes": {"proj": [{"channel": "feishu-cardkit"}]}}
        with mock.patch.object(feige, "load_routes", return_value=doc):
            self.assertTrue(common.routes_auto_enabled("proj"))
            self.assertFalse(common.routes_auto_enabled("other"))

    def test_run_skips_when_no_route_and_hook_off(self):
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        called = []
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(common, "apply_plugin_options", lambda *a, **k: None), \
             mock.patch.object(common, "routes_auto_enabled", return_value=False):
            common.run(lambda: called.append(1), project="proj")
        self.assertEqual(called, [])

    def test_run_fires_when_route_present_even_hook_off(self):
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        called = []
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(common, "apply_plugin_options", lambda *a, **k: None), \
             mock.patch.object(common, "routes_auto_enabled", return_value=True):
            common.run(lambda: called.append(1), project="proj")
        self.assertEqual(called, [1])

    def test_global_off_still_wins_without_route(self):
        """没配路由时全局开关语义不变：FEIGE_HOOK_NOTIFY=1 才发。"""
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        called = []
        with mock.patch.dict(os.environ, {"FEIGE_HOOK_NOTIFY": "1"}, clear=True), \
             mock.patch.object(common, "apply_plugin_options", lambda *a, **k: None):
            common.run(lambda: called.append(1))
        self.assertEqual(called, [1])


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

    def test_body_over_min_chars_gets_summarized_even_under_limit(self):
        """启用总结后，中等长度（未超渠道上限）的冗长正文也要浓缩，不再原样镜像。"""
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test")
        mid = "冗长战报" * 100  # 400 字：低于 BODY_MAX_BYTES，高于默认 min_chars=300
        self.assertLess(len(mid.encode("utf-8")), feige.BODY_MAX_BYTES)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize", return_value="浓缩版") as ls:
            self.assertEqual(feige.fit_body(mid), "浓缩版")
        ls.assert_called_once()

    def test_body_under_min_chars_stays_verbatim(self):
        """低于触发阈值的短正文保持原样，不为它多花一次 LLM 调用。"""
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test")
        short = "短正文" * 50  # 150 字 < 300
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize") as ls:
            self.assertEqual(feige.fit_body(short), short)
        ls.assert_not_called()

    def test_min_chars_zero_restores_over_limit_only(self):
        """min_chars=0 回到旧口径：只有超渠道上限才总结。"""
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-test", FEIGE_LLM_MIN_CHARS="0")
        mid = "冗长战报" * 100
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize") as ls:
            self.assertEqual(feige.fit_body(mid), mid)  # 未超上限：原样
        ls.assert_not_called()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(feige, "llm_summarize", return_value="浓缩版"), \
                mock.patch.object(feige, "clip", return_value="浓缩版"):
            self.assertEqual(feige.fit_body(self.LONG), "浓缩版")  # 超上限：仍总结

    def test_llm_config_reads_min_chars(self):
        env, settings = self._clean_env(FEIGE_LLM_MIN_CHARS="120")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(feige.llm_config()["min_chars"], 120)
        env2 = {k: v for k, v in env.items() if k != "FEIGE_LLM_MIN_CHARS"}
        settings.write_text(json.dumps({"llm_api_key": "sk-x", "llm_min_chars": "500"}),
                            encoding="utf-8")
        with mock.patch.dict(os.environ, env2, clear=True):  # 环境变量没设：设置文件生效
            self.assertEqual(feige.llm_config()["min_chars"], 500)

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

    @staticmethod
    def _fake_llm(content, finish_reason="stop"):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"choices": [
                {"message": {"content": content}, "finish_reason": finish_reason}]}).encode()
        return FakeResp()

    def test_llm_summarize_extracts_tagged_summary_amid_monologue(self):
        # 1.0.2 群里翻车现场：复述要求 + 独白 + 真摘要 + 截断残句被整段镜像进卡片
        raw = (
            "用户要求把冗长的收尾汇报压缩成不超过400字的中文摘要，只输出摘要本身。\n"
            "我需要把上面的汇报精简，抓住核心信息：\n"
            "- 旧分析要点\n"
            "让我组织一下：\n"
            "<summary>- **协议**：Qoder 双 provider\n"
            "- **验证**：513/513 通过</summary>\n"
            "检查字数，应该在400字以内。让我数一下大概。\n"
            "这个内容比较"
        )
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen",
                           return_value=self._fake_llm(raw, finish_reason="length")):
            out = feige.llm_summarize("原文")
        self.assertEqual(out, "- **协议**：Qoder 双 provider\n- **验证**：513/513 通过")

    def test_llm_summarize_fallback_strips_monologue_without_tags(self):
        raw = (
            "用户要求把汇报压缩成摘要，只输出摘要本身。\n"
            "我需要把汇报精简，抓住核心信息：\n"
            "- **协议**：Qoder 双 provider\n"
            "- **验证**：513/513 通过\n"
            "检查字数，应该在400字以内。让我数一下大概。\n"
            "这个内容比较"
        )
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen",
                           return_value=self._fake_llm(raw, finish_reason="length")):
            out = feige.llm_summarize("原文")
        self.assertIn("**协议**：Qoder", out)
        self.assertIn("513/513", out)
        self.assertNotIn("用户要求", out)
        self.assertNotIn("我需要", out)
        self.assertNotIn("检查字数", out)
        self.assertNotIn("这个内容", out)

    def test_llm_summarize_unclosed_tag_takes_residual(self):
        raw = "让我组织一下：\n<summary>- **协议**：Qoder 双 provider\n- **验证**：通过"
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=self._fake_llm(raw)):
            out = feige.llm_summarize("原文")
        self.assertEqual(out, "- **协议**：Qoder 双 provider\n- **验证**：通过")

    def test_llm_summarize_echoed_tag_pair_is_not_the_summary(self):
        # 复述要求里带的标签对捕出来只有残渣 → 剥独白 → 剥完为空 → fit_body 硬截断兜底
        raw = ("用户要求把汇报压缩成摘要，放在 <summary> 与 </summary> 标签之间，"
               "只输出摘要本身。")
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=self._fake_llm(raw)):
            self.assertEqual(feige.llm_summarize("原文"), "")
            out = feige.fit_body(self.LONG)
        self.assertIn("内容过长已截断", out)

    def test_llm_summarize_request_max_tokens_and_direct_prompt(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return self._fake_llm("<summary>• 结论：成功</summary>")

        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x", FEIGE_LLM_MAX_CHARS="400")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            feige.llm_summarize("原文")
        self.assertEqual(captured["body"]["max_tokens"], 2400)
        sys_prompt = captured["body"]["messages"][0]["content"]
        self.assertIn("严禁输出任何思考过程", sys_prompt)
        self.assertIn("直接输出最终结果", sys_prompt)
        self.assertIn("一句话结论置顶", sys_prompt)
        self.assertIn("核心改动", sys_prompt)
        self.assertIn("验证结果", sys_prompt)
        self.assertIn("后续待办", sys_prompt)
        self.assertIn("上下级", sys_prompt)
        self.assertIn("加粗", sys_prompt)

    def test_llm_summarize_hierarchical_structure_and_chapters(self):
        # 章节与上下级层次排版结构提取（一句话结论置顶 + 核心改动 + 验证结果 + 后续待办）
        raw = (
            "<think>正在提炼章节与上下级结构...</think>\n"
            "<summary>\n"
            "**结论**：**feige-fry-cards** 总结提示词已优化并引入章节层次。\n\n"
            "**核心改动**：\n"
            "- **提示词重构**：修改 `feige.py` 系统提示词，规范章节与上下级输出。\n"
            "- **排版增强**：强调核心指标加粗与单空行段落分隔。\n\n"
            "**验证结果**：\n"
            "- **单元测试**：`pytest` 241/241 全部通过。\n\n"
            "**后续待办**：\n"
            "- 观察真实群卡片渲染样式。\n"
            "</summary>\n"
            "检查字数，约 180 字。"
        )
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=self._fake_llm(raw)):
            out = feige.llm_summarize("原文")
        self.assertIn("**结论**：**feige-fry-cards**", out)
        self.assertIn("**核心改动**：", out)
        self.assertIn("- **提示词重构**：", out)
        self.assertIn("**验证结果**：", out)
        self.assertIn("- **单元测试**：`pytest` 241/241", out)
        self.assertIn("**后续待办**：", out)
        self.assertNotIn("检查字数", out)
        self.assertNotIn("正在提炼", out)
        # 验证章节之间保留了段落空行，避免粘连挤在一起
        self.assertIn("**核心改动**：\n- **提示词重构**", out)
        self.assertIn("\n\n**验证结果**：", out)

    def test_strip_monologue_preserves_single_blank_lines_between_sections(self):
        text = (
            "我先总结一下：\n"
            "\n\n"
            "**结论**：已完成全部任务。\n"
            "\n\n\n"
            "**核心改动**：\n"
            "- 修改了配置。\n"
            "\n"
            "检查字数，没有超标。\n"
            "\n"
            "**验证结果**：\n"
            "- 测试全部通过。\n"
            "\n\n"
        )
        cleaned = feige._strip_monologue(text)
        expected = (
            "**结论**：已完成全部任务。\n\n"
            "**核心改动**：\n"
            "- 修改了配置。\n\n"
            "**验证结果**：\n"
            "- 测试全部通过。"
        )
        self.assertEqual(cleaned, expected)

    def test_llm_summarize_strips_think_tags_and_reasoning_cot(self):
        # 推理模型（DeepSeek-R1 / GLM / hy4）输出显式 <think> 标签或未闭合思考
        raw = (
            "<think>\n"
            "正在思考如何总结...\n"
            "字数控制在400字以内，提取要点。\n"
            "</think>\n"
            "<summary>\n"
            "• 结论：已完成替换\n"
            "• 验证：测试全部通过\n"
            "</summary>"
        )
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=self._fake_llm(raw)):
            out = feige.llm_summarize("原文")
        self.assertEqual(out, "• 结论：已完成替换\n• 验证：测试全部通过")

    def test_llm_summarize_user_bug_reproduction_nested_summary_and_monologue(self):
        # 用户现场真实翻车 case：模型在开头加 <summary>，随后进行独白与字数估算，再开第二个 <summary>
        raw = (
            "<summary>\n"
            "标签内。考虑是否可以用markdown列表，在标签内可以有“-”。不要写“标签之外不得输出”。最终不要有分析。字数估算：约三百多中文字符。需保留文件/命令名和数字，尽量完整。\n\n"
            "我也要注意“推 main”可能写成“已提交并推 main”。“CI 当前运行中 run 45973926776”。“回归门36个已知失败项无新增恶化”。“发版须用户确认”。“本地与 origin/main 对齐、无未提交改动”。“可选事项”压缩为一行。\n\n"
            "摘要草稿：\n"
            "<summary>\n"
            "• 结论：AutoClaw 双线官方图标替换已完成并提交推 main；本地与 origin/main 对齐，无未提交改动。\n"
            "• 关键改动：国际版 `autoclaw.png` 换为官方白底黑 a 128x128 栅格（3524B，SHA1 `98cd8d00864f`）；国内版新增 `autoclaw-cn.png` 官方橙底米白 a 128x128（8622B，SHA1 `1dfc1d5184be`）。\n"
            "• 验证：图标测试 5/5 绿；CI 运行中（run `45973926776`）。\n"
            "</summary>"
        )
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=self._fake_llm(raw)):
            out = feige.llm_summarize("原文")
        self.assertTrue(out.startswith("• 结论：AutoClaw"))
        self.assertNotIn("标签内", out)
        self.assertNotIn("摘要草稿", out)
        self.assertNotIn("<summary>", out)
        self.assertNotIn("我也要注意", out)

    def test_llm_summarize_strips_monologue_inside_single_summary_tag(self):
        # 标签内包裹独白时二次清洗
        raw = (
            "<summary>\n"
            "标签内。考虑是否可以用markdown列表。\n"
            "我也要注意保留文件路径与验证信息。\n"
            "摘要草稿：\n"
            "• 结论：接口重构已合入 main\n"
            "• 验证：12/12 通过\n"
            "</summary>\n"
            "检查字数，符合不超过400字的要求。"
        )
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=self._fake_llm(raw)):
            out = feige.llm_summarize("原文")
        self.assertEqual(out, "• 结论：接口重构已合入 main\n• 验证：12/12 通过")
        self.assertNotIn("标签内", out)
        self.assertNotIn("检查字数", out)

    def test_llm_summarize_pure_thinking_falls_back_to_clip(self):
        # 思考模型在输出摘要前截断（仅产生 <think> 思考块）→ 兜底截断原文，绝不发思考内容
        raw = "<think>\n我正在仔细阅读并精简汇报...\n用户要求压缩在400字内...\n"
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-x")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen",
                           return_value=self._fake_llm(raw, finish_reason="length")):
            self.assertEqual(feige.llm_summarize("原文"), "")
            out = feige.fit_body(self.LONG)
        self.assertIn("内容过长已截断", out)
        self.assertNotIn("我正在仔细阅读", out)

    def test_redact_covers_llm_key(self):
        env, _ = self._clean_env(FEIGE_LLM_API_KEY="sk-supersecret01")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn("sk-supersecret01", feige.redact("boom sk-supersecret01"))


class TranscriptRefreshTest(unittest.TestCase):
    """后台发送进程开卡前重读转录：治宿主落盘滞后导致的「战报落后一轮」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FEIGE_")}
        env.update({"FEIGE_SETTINGS_FILE": str(Path(self.tmp.name, "s.json")),
                    "FEIGE_READ_BODY_DELAY": "0"})  # 测试不等延迟
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def transcript(self, entries, name="t.jsonl") -> str:
        p = Path(self.tmp.name, name)
        p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
                     encoding="utf-8")
        return str(p)

    @staticmethod
    def _asst(text, ts="2026-09-26T00:00:10Z"):
        return {"type": "assistant", "timestamp": ts,
                "message": {"model": "m1",
                            "content": [{"type": "text", "text": text}]}}

    def test_refresh_picks_up_late_final_text(self):
        """转录里已有本轮文本而 hook 只截到上一轮（落盘滞后）→ 重读刷新成本轮。"""
        tr = self.transcript([
            self._asst("上一轮旧文", ts="2026-09-26T00:00:02Z"),
            {"type": "user", "timestamp": "2026-09-26T00:00:05Z",
             "message": {"content": "新提问"}},
            self._asst("本轮最终战报"),
        ])
        args = mock.Mock(body="上一轮旧文", transcript=tr, dry_run=False,
                         model="old", thinking=0, tools=0, context="", tokens=0, elapsed="")
        fresh = feige.refresh_body_from_transcript(args)
        self.assertEqual(fresh, len("本轮最终战报"))
        self.assertEqual(args.body, "本轮最终战报")
        self.assertEqual(args.model, "claude-code · m1")  # 统计一并同步

    def test_refresh_skips_placeholder_body(self):
        """重读到占位符（新一轮已开始、文本未落盘）→ 不替换，保持 hook 正文。"""
        tr = self.transcript([
            {"type": "user", "timestamp": "2026-09-26T00:00:20Z",
             "message": {"content": "新提问"}},
            self._asst("上一轮旧文", ts="2026-09-26T00:00:10Z"),
        ])
        args = mock.Mock(body="上一轮旧文", transcript=tr)
        self.assertEqual(feige.refresh_body_from_transcript(args), 0)
        self.assertEqual(args.body, "上一轮旧文")

    def test_refresh_missing_or_identical_noop(self):
        args = mock.Mock(body="x", transcript=str(Path(self.tmp.name, "nope.jsonl")))
        self.assertEqual(feige.refresh_body_from_transcript(args), 0)
        self.assertEqual(args.body, "x")
        tr = self.transcript([self._asst("同文")])
        args = mock.Mock(body="同文", transcript=tr)  # 相同 → 无刷新空间
        self.assertEqual(feige.refresh_body_from_transcript(args), 0)
        self.assertEqual(args.body, "同文")

    def test_run_send_passes_transcript_to_payload(self):
        """--transcript 时 run_send 会触发刷新并照常发送（端到端，发到本地捕获服务器）。"""
        tr = self.transcript([self._asst("重读后的战报")])
        handler = type("H", (_Capture,), {"got": [], "delay": 0.0})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        os.environ["FEISHU_CARD_WEBHOOK"] = f"http://127.0.0.1:{srv.server_address[1]}/hook"
        argv = ["send", "--title=t", "--body=旧文", "--webhook=" + os.environ["FEISHU_CARD_WEBHOOK"],
                f"--transcript={tr}", "--debounce=0"]
        args = feige._build_parser().parse_args(argv)
        rc = feige.run_send(args)
        self.assertEqual(rc, 0)
        end = time.time() + 5
        while time.time() < end and not handler.got:
            time.sleep(0.05)
        self.assertEqual(len(handler.got), 1)
        self.assertIn("重读后的战报", json.dumps(handler.got[0][1], ensure_ascii=False))

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
            (home / ".gemini" / "config").mkdir(parents=True)
            (home / ".gemini" / "config" / "hooks.json").write_text(
                '{"feige": {"Stop": [{"type": "command", "command": \'python "C:/repo/adapters/antigravity/stop_notify.py"\'}]}}',
                encoding="utf-8")
            html_ = webui.page_adapters(home).decode("utf-8")
        self.assertEqual(html_.count("已安装 feige-fry-cards 插件"), 3)
        self.assertIn("已手挂 Stop hook", html_)
        self.assertIn("已配置全局 Stop hook", html_)
        self.assertNotIn("未接入", html_)


class PluginOptionsTest(unittest.TestCase):
    def test_mapping_real_env_wins(self):
        sys.path.insert(0, str(REPO / "adapters"))
        import common
        env = {"CLAUDE_PLUGIN_OPTION_HOOK_NOTIFY": "true",
               "CLAUDE_PLUGIN_OPTION_WEBHOOK_URL": "https://plugin/hook",
               "CLAUDE_PLUGIN_OPTION_LLM_MIN_CHARS": "500",
               "ZCODE_USER_CONFIG_APP_ID": "cli_z",
               "FEISHU_NOTIFY_CHAT_ID": "oc_real", "CLAUDE_PLUGIN_OPTION_NOTIFY_CHAT_ID": "oc_plugin"}
        common.apply_plugin_options(env)
        self.assertEqual(env["FEIGE_HOOK_NOTIFY"], "true")
        self.assertEqual(env["FEISHU_CARD_WEBHOOK"], "https://plugin/hook")
        self.assertEqual(env["FEISHU_APP_ID"], "cli_z")
        self.assertEqual(env["FEISHU_NOTIFY_CHAT_ID"], "oc_real")
        self.assertEqual(env["FEIGE_LLM_MIN_CHARS"], "500")


class LoopbackProxyBypassTest(E2EBase):
    def test_post_json_dials_loopback_despite_proxy(self):
        """回归：HTTP_PROXY 拦一切 http:// 时，发往 127.0.0.1 的 webhook 仍直连送达。

        代理环境（HTTP_PROXY 指到死端口）下不走直连就会 502/连接拒绝。"""
        dead = "http://127.0.0.1:9"  # discard 端口，连接必失败
        with mock.patch.dict(os.environ, {"HTTP_PROXY": dead, "HTTPS_PROXY": dead,
                                          "http_proxy": dead, "https_proxy": dead}):
            ok, detail = feige.send_report("t", "loopback 直连", channel="feishu-webhook",
                                           webhook=self.url + "/hook")
        self.assertTrue(ok, detail)
        self.assertEqual(len(self.handler.got), 1)


class KimiTranscriptE2ETest(E2EBase):
    """kimi 全链路：wire.jsonl 交给后台发送进程重读（接线回归）。"""

    @staticmethod
    def _loop(t, turn, etype, **kw):
        event = {"type": etype, "turnId": turn}
        event.update(kw)
        return json.dumps({"type": "context.append_loop_event", "agentId": "main",
                           "time": t, "event": event})

    def test_kimi_refresh_picks_up_late_text(self):
        """hook 时 wire 还没有本轮文本（占位符正文）→ 落盘后重读 → 卡片带迟到的正文。"""
        home = Path(self.tmp.name, "kimi")
        wire = home / "sessions" / "wd_x" / "session_k" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text("\n".join([
            self._loop(1_790_000_000_000, "1", "content.part", part={"type": "think", "think": "…"}),
            json.dumps({"type": "usage.record", "agentId": "main", "time": 1_790_000_001_000,
                        "usage": {"inputOther": 100, "inputCacheRead": 0, "inputCacheCreation": 0,
                                  "output": 100}, "usageScope": "turn"}),
        ]) + "\n", encoding="utf-8")
        self.env["FEISHU_CARD_WEBHOOK"] = self.url + "/hook"
        self.env["FEIGE_READ_BODY_DELAY"] = "3"  # 重读推迟到 3s，给补写正文留足时间
        payload = {"client_type": "kimi_code_cli", "session_id": "session_k", "cwd": "/x/demo"}
        r = subprocess.run([sys.executable, str(REPO / "hooks" / "stop.py")],
                           input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                           env={**self.env, "KIMI_CODE_HOME": str(home)}, encoding="utf-8",
                           timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        # 后台发送进程 1.5s 后重读转录；在此之前把收尾文本补写进 wire（落盘滞后模拟）
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(self._loop(1_790_000_002_000, "1", "content.part",
                                part={"type": "text", "text": "迟到的收尾正文"}) + "\n")
        got = self.wait_for(1)
        card = json.dumps(got[0][1], ensure_ascii=False) if got else ""
        self.assertIn("迟到的收尾正文", card, r.stderr)


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
