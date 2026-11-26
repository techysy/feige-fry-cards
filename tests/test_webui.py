"""Offline tests for WebUI pages, route editing, and HTTP guards."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import feige  # noqa: E402
import webui  # noqa: E402


class WebuiPagesTest(unittest.TestCase):
    def test_page_escapes_title_and_includes_generated_markup(self):
        rendered = webui.page('<title>', '<b>generated markup</b>').decode("utf-8")
        self.assertIn("&lt;title&gt;", rendered)
        self.assertIn("<b>generated markup</b>", rendered)

    def test_channel_status_exposes_only_booleans(self):
        with mock.patch.object(webui.feige, "webhook_url", return_value="https://secret.invalid/key"), \
                mock.patch.object(webui.feige, "_env", side_effect=lambda name, *aliases: {
                    "FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret",
                    "DINGTALK_WEBHOOK": "ding-key", "TELEGRAM_BOT_TOKEN": "bot-token",
                    "TELEGRAM_CHAT_ID": "chat-id",
                }.get(name, "")):
            result = webui.channel_status()

        self.assertEqual(result, {
            "feishu-webhook": True,
            "feishu-cardkit": True,
            "dingtalk-webhook": True,
            "telegram": True,
        })
        self.assertTrue(all(type(value) is bool for value in result.values()))

    def test_studio_shows_configuration_without_secret_values(self):
        marker = "never-render-this-secret"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FEIGE_", "FEISHU_"))}
        env["FEISHU_CARD_WEBHOOK"] = marker
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(webui.feige, "routes_file_path", return_value="routes.json"), \
                mock.patch.object(webui.feige, "load_routes", return_value=None):
            rendered = webui.page_studio().decode("utf-8")

        self.assertIn("飞书渠道", rendered)
        self.assertNotIn(marker, rendered)  # webhook 值绝不回显，只显已配置徽章

    def test_studio_grid_is_masonry_columns_not_row_grid(self):
        # 行式 grid 行高被该行最高卡撑开，矮卡下吊空白、整页超一屏；
        # 配置页必须是多列瀑布流：每列独立堆叠，卡片不跨列断开
        rendered = webui.page_studio().decode("utf-8")
        self.assertIn(".grid{columns:420px", rendered)
        self.assertIn(".grid>.card{break-inside:avoid", rendered)
        self.assertNotIn(".grid{display:grid", rendered)

    def test_nav_aligns_with_card_left_edge(self):
        # 导航左缘 = 居中列外缘 + 22px 内边距，与卡片左缘齐平（此前凸出 22px）
        rendered = webui.page_studio().decode("utf-8")
        self.assertIn("calc((100% - var(--col-w))/2 + 22px)", rendered)

    def test_warn_banner_truncates_on_narrow_screens(self):
        # 非宽屏提示单行截断（不再挤四排），宽屏恢复完整换行
        rendered = webui.page_studio().decode("utf-8")
        self.assertIn(".warn-line{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}", rendered)
        self.assertIn("@media (min-width:1420px){.warn-line{white-space:normal;overflow:visible}}", rendered)
        self.assertEqual(rendered.count("class=\"warn-line\""), 2)

    def test_studio_home_has_no_duplicate_routes_card(self):
        # 群路由有独立页签，主页底部不再重复挂入口卡
        rendered = webui.page_studio().decode("utf-8")
        self.assertNotIn("编辑群路由", rendered)
        self.assertIn("/routes", rendered)  # 页签导航入口仍在

    def test_status_page_lists_channels_and_test_buttons(self):
        rendered = webui.page_status().decode("utf-8")
        for ch in webui.feige.CHANNELS:
            self.assertIn(ch, rendered)
        self.assertIn("发送测试卡", rendered)

    def test_routes_form_escapes_user_values(self):
        doc = {"routes": {"<project>": [{
            "channel": "feishu-webhook", "webhook": "<img src=x>",
        }]}}
        rendered = webui._routes_form(doc).decode("utf-8")

        self.assertIn("&lt;project&gt;", rendered)
        self.assertIn("&lt;img src=x&gt;", rendered)
        self.assertNotIn("<img src=x>", rendered)

    def test_preview_dry_run_escapes_user_content(self):
        query = urllib.parse.parse_qs(urllib.parse.urlencode({
            "go": "1", "title": "<script>title</script>",
            "body": "<img src=x>", "status": "running", "thinking": "2", "tools": "3",
        }))
        with mock.patch.object(webui.feige, "send_report", return_value=(True, '{"ok":true}')) as send:
            rendered = webui.page_preview(query).decode("utf-8")

        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["dry_run"])
        self.assertIn("&lt;script&gt;title&lt;/script&gt;", rendered)
        self.assertIn("&lt;img src=x&gt;", rendered)
        self.assertNotIn("<script>title</script>", rendered)
        self.assertNotIn("<img src=x>", rendered)


class RoutesFormTest(unittest.TestCase):
    def test_parse_form_skips_empty_rows_and_preserves_env_references(self):
        form = {
            "proj_0": [" project-a "],
            "ch_0_0": ["dingtalk-webhook"], "wh_0_0": ["$DINGTALK_WEBHOOK"],
            "sec_0_0": ["$DINGTALK_SECRET"],
            "ch_0_1": [""], "wh_0_1": [""],
            "proj_1": ["*"],
        }
        doc, error = webui._parse_routes_form(form)

        self.assertEqual(error, "")
        self.assertEqual(doc, {"routes": {
            "project-a": [{"channel": "dingtalk-webhook", "webhook": "$DINGTALK_WEBHOOK",
                           "secret": "$DINGTALK_SECRET"}],
            "*": [],
        }})

    def test_parse_form_rejects_target_without_valid_channel(self):
        form = {"proj_0": ["project-a"], "wh_0_0": ["https://hook.invalid"]}
        doc, error = webui._parse_routes_form(form)

        self.assertIsNone(doc)
        self.assertIn("channel", error)

    def test_save_routes_creates_backup_and_keeps_env_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            routes_file = Path(temp_dir) / "routes.json"
            routes_file.write_text('{"routes": {}}\n', encoding="utf-8")
            body = urllib.parse.urlencode({
                "proj_0": "project-a", "ch_0_0": "dingtalk-webhook",
                "wh_0_0": "$DINGTALK_WEBHOOK", "sec_0_0": "$DINGTALK_SECRET",
            }).encode("utf-8")
            with mock.patch.object(webui.feige, "routes_file_path", return_value=str(routes_file)), \
                    mock.patch.object(webui.feige, "load_routes", return_value={"routes": {}}):
                rendered = webui.handle_routes_post(body).decode("utf-8")

            saved = json.loads(routes_file.read_text(encoding="utf-8"))
            backup = json.loads(Path(str(routes_file) + ".bak").read_text(encoding="utf-8"))

        self.assertEqual(saved["routes"]["project-a"][0]["webhook"], "$DINGTALK_WEBHOOK")
        self.assertEqual(saved["routes"]["project-a"][0]["secret"], "$DINGTALK_SECRET")
        self.assertEqual(backup, {"routes": {}})
        self.assertIn("已保存", rendered)

    def test_save_failure_is_rendered_as_error(self):
        body = urllib.parse.urlencode({"proj_0": "project-a"}).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            routes_file = Path(temp_dir) / "routes.json"
            with mock.patch.object(webui.feige, "routes_file_path", return_value=str(routes_file)), \
                    mock.patch.object(webui.feige, "load_routes", return_value={"routes": {}}), \
                    mock.patch.object(webui.os, "replace", side_effect=OSError("disk unavailable")):
                rendered = webui.handle_routes_post(body).decode("utf-8")

        self.assertIn("保存失败", rendered)
        self.assertIn("disk unavailable", rendered)


class TestSendHandlerTest(unittest.TestCase):
    def test_unconfigured_channel_does_not_send(self):
        body = urllib.parse.urlencode({"channel": "feishu-webhook"}).encode("utf-8")
        with mock.patch.object(webui, "channel_status", return_value={"feishu-webhook": False}), \
                mock.patch.object(webui.feige, "send_report") as send:
            rendered = webui.handle_test_post(body).decode("utf-8")

        send.assert_not_called()
        self.assertIn("未配置", rendered)

    def test_configured_channel_sends_fixed_card_and_redacts_result(self):
        body = urllib.parse.urlencode({"channel": "feishu-webhook"}).encode("utf-8")
        with mock.patch.object(webui, "channel_status", return_value={"feishu-webhook": True}), \
                mock.patch.object(webui.feige, "send_report", return_value=(False, "failed SECRET_VALUE")) as send, \
                mock.patch.object(webui.feige, "redact", return_value="failed [REDACTED]"):
            rendered = webui.handle_test_post(body).decode("utf-8")

        self.assertEqual(send.call_args.kwargs["channel"], "feishu-webhook")
        self.assertIn("failed [REDACTED]", rendered)
        self.assertNotIn("SECRET_VALUE", rendered)


class WebuiHttpTest(unittest.TestCase):
    def setUp(self):
        self.server = webui.Server(("127.0.0.1", 0), webui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, response.getheader("Content-Type"), response.read().decode("utf-8")
        conn.close()
        return result

    def test_overview_http_response(self):
        with mock.patch.object(webui, "page_studio", return_value=b"studio"):
            status, content_type, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertEqual(body, "studio")

    def test_cross_site_post_is_rejected_before_dispatch(self):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://evil.example",
        }
        with mock.patch.object(webui, "handle_routes_post") as handler:
            status, _, body = self.request("POST", "/routes", body=b"proj_0=x", headers=headers)

        self.assertEqual(status, 403)
        self.assertIn("跨站请求被拒绝", body)
        handler.assert_not_called()

    def test_dns_rebinding_host_is_rejected(self):
        headers = {"Host": f"evil.example:{self.port}"}
        status, _, body = self.request("GET", "/", headers=headers)

        self.assertEqual(status, 403)
        self.assertIn("Host 不被允许", body)


class StatsScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "s.json"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_", "DINGTALK_", "TELEGRAM_"))}
        env["FEIGE_SETTINGS_FILE"] = str(self.settings)
        self._patch = mock.patch.dict(os.environ, env, clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_scope_resolution_order(self):
        """env FEIGE_STATS_SCOPE > 设置文件 > 默认 turn。"""
        self.assertEqual(feige.stats_scope(), "turn")
        self.settings.write_text('{"stats_scope": "session"}', encoding="utf-8")
        self.assertEqual(feige.stats_scope(), "session")
        self.settings.write_text('{"stats_scope": "bogus"}', encoding="utf-8")
        self.assertEqual(feige.stats_scope(), "turn")
        with mock.patch.dict(os.environ, {"FEIGE_STATS_SCOPE": "session"}):
            self.assertEqual(feige.stats_scope(), "session")

    def test_handle_settings_post(self):
        resp = webui.handle_settings_post(b"form=behavior&stats_scope=session").decode("utf-8")
        self.assertIn("行为开关已保存", resp)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["stats_scope"], "session")
        self.assertFalse(Path(str(self.settings) + ".tmp").exists())  # 原子写不留临时文件
        # 首次保存没有旧文件可备份；第二次才产生 .bak
        webui.handle_settings_post(b"form=behavior&stats_scope=turn&debounce_seconds=5")
        self.assertEqual(json.loads(Path(str(self.settings) + ".bak").read_text(encoding="utf-8"))["stats_scope"],
                         "session")
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["stats_scope"], "turn")
        resp = webui.handle_settings_post(b"form=behavior&stats_scope=bogus").decode("utf-8")
        self.assertIn("非法统计口径", resp)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["stats_scope"], "turn")

    def test_secret_keys_never_render_and_keep_on_empty(self):
        """密钥：保存后页面不回显值；留空不修改；勾选清除才删。"""
        body = urllib.parse.urlencode({"form": "llm", "llm_api_key": "sk-studio-1"}).encode()
        resp = webui.handle_settings_post(body).decode("utf-8")
        self.assertIn("正文总结已保存", resp)
        self.assertNotIn("sk-studio-1", resp)
        doc = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(doc["llm_api_key"], "sk-studio-1")
        # 空提交：保持现状
        webui.handle_settings_post(b"form=llm")
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["llm_api_key"],
                         "sk-studio-1")
        # 清除
        webui.handle_settings_post(b"form=llm&clear_llm_api_key=1")
        self.assertNotIn("llm_api_key", json.loads(self.settings.read_text(encoding="utf-8")))
        page = webui.page_studio().decode("utf-8")
        self.assertNotIn("sk-studio-1", page)

    def test_studio_page_shows_current_values_and_badges(self):
        self.settings.write_text(json.dumps({"feishu_app_id": "cli_123",
                                             "llm_api_key": "sk-page-keep",
                                             "llm_model": "glm-4", "stats_scope": "session"}),
                                 encoding="utf-8")
        page = webui.page_studio().decode("utf-8")
        self.assertIn("cli_123", page)          # 非敏感值回显
        self.assertIn("glm-4", page)
        self.assertNotIn("sk-page-keep", page)  # 密钥绝不回显
        self.assertIn("已配置 · 设置文件", page)
        self.assertIn("value='session' selected", page)
        # 环境变量优先来源标注
        os.environ["FEISHU_APP_ID"] = "cli_env"
        page = webui.page_studio().decode("utf-8")
        self.assertIn("已配置 · 环境变量", page)

    def test_unknown_card_and_validation_errors(self):
        resp = webui.handle_settings_post(b"form=nope").decode("utf-8")
        self.assertIn("未知设置卡片", resp)
        resp = webui.handle_settings_post(b"form=llm&llm_max_chars=abc").decode("utf-8")
        self.assertIn("非法摘要目标字数", resp)
        resp = webui.handle_settings_post(b"form=llm&llm_min_chars=abc").decode("utf-8")
        self.assertIn("非法总结触发字数", resp)
        resp = webui.handle_settings_post(b"form=behavior&debounce_seconds=x").decode("utf-8")
        self.assertIn("非法去抖秒数", resp)

    def test_llm_min_chars_save_and_page(self):
        """总结触发字数：保存校验（≥0）、留空回默认、页面渲染提示文案。"""
        webui.handle_settings_post(b"form=llm&llm_min_chars=500")
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["llm_min_chars"], "500")
        webui.handle_settings_post(b"form=llm&llm_min_chars=0")  # 0 合法：只在超上限才总结
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["llm_min_chars"], "0")
        webui.handle_settings_post(b"form=llm")  # 留空 = 删键回默认 300
        self.assertNotIn("llm_min_chars", json.loads(self.settings.read_text(encoding="utf-8")))
        page = webui.page_studio().decode("utf-8")
        self.assertIn("总结触发字数", page)
        self.assertIn("llm_min_chars", page)

    def test_panel_and_footer_chips_save_independently(self):
        """面板头/脚注两套 chips + 脚注显隐开关：交互卡白名单内独立保存。"""
        page = webui.page_studio().decode("utf-8")
        self.assertIn("统一面板头字段（panel）", page)
        self.assertIn("底部脚注字段（footer）", page)
        self.assertIn("显示底部统计脚注行", page)
        self.assertIn("panel_fields-chips-box", page)
        self.assertIn("footer_fields-chips-box", page)
        # 面板默认不含项目 chip 激活；脚注默认全字段激活
        self.assertIn("name='panel_fields' id='panel_fields_val' value='model,thinking,tools,context,tokens,elapsed'", page)
        self.assertIn("name='footer_fields' id='footer_fields_val' value='project,model,thinking,tools,context,tokens,elapsed'", page)

        body = urllib.parse.urlencode({
            "form": "interactive",
            "panel_fields": "model,tools",
            "footer_enabled": "1",
            "footer_fields": "project,elapsed",
        }).encode("utf-8")
        resp = webui.handle_settings_post(body).decode("utf-8")
        self.assertIn("交互与长连接已保存", resp)
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(saved["panel_fields"], ["model", "tools"])
        self.assertEqual(saved["footer_fields"], ["project", "elapsed"])
        self.assertEqual(saved["footer_enabled"], "1")
        self.assertEqual(feige._panel_field_order(), ("model", "tools"))
        self.assertTrue(feige.footer_enabled())
        # 不勾开关再存：显式写 0（关），字段配置保留
        webui.handle_settings_post(b"form=interactive&panel_fields=model&footer_fields=")
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(saved["footer_enabled"], "0")
        self.assertEqual(saved["panel_fields"], ["model"])
        self.assertEqual(saved["footer_fields"], [])  # 空 = 全隐藏（合法）
        self.assertFalse(feige.footer_enabled())

    def test_llm_off_toggle_writes_and_clears_summarize(self):
        webui.handle_settings_post(b"form=llm&llm_api_key=sk-1&llm_off=1")
        self.assertIs(json.loads(self.settings.read_text(encoding="utf-8"))["summarize"], False)
        webui.handle_settings_post(b"form=llm")  # 不勾 = 回到跟随 key
        self.assertNotIn("summarize", json.loads(self.settings.read_text(encoding="utf-8")))

    def test_fetch_models_api_endpoint(self):
        """测试 /api/llm/models 路由：成功与错误返回标准 JSON 结构。"""
        server = webui.Server(("127.0.0.1", 0), webui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        mock_models = [
            {"id": "openai/gpt-4o", "owned_by": "openai", "context_window": 128000},
            {"id": "qwen/qwen-2.5", "owned_by": "alibaba", "context_window": 32000}
        ]

        try:
            with mock.patch.object(feige, "fetch_models", return_value=mock_models):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                payload = json.dumps({"base_url": "https://api.openai.com/v1", "api_key": "sk-test"}).encode("utf-8")
                conn.request("POST", "/api/llm/models", body=payload,
                             headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data.get("ok"))
                self.assertEqual(len(data.get("models")), 2)
                self.assertEqual(data["models"][0]["id"], "openai/gpt-4o")
                conn.close()

            # 异常时返回 502 并携带错误信息
            with mock.patch.object(feige, "fetch_models", side_effect=RuntimeError("服务拒绝连接")):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                payload = json.dumps({"base_url": "https://bad.url"}).encode("utf-8")
                conn.request("POST", "/api/llm/models", body=payload,
                             headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 502)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(data.get("ok"))
                self.assertIn("服务拒绝连接", data.get("error"))
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class StudioKeepAliveTest(unittest.TestCase):
    """后台保活：pidfile 读写、探活、启停编排、supervise 裁决（全离线 mock）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("FEIGE_STUDIO_")}
        env["FEIGE_STUDIO_PID_FILE"] = str(Path(self.tmp.name, "studio.json"))
        env["FEIGE_STUDIO_LOG_FILE"] = str(Path(self.tmp.name, "studio.log"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_supervise_decision(self):
        # worker 绑不上端口且已有人接管 → 守护功成身退
        self.assertEqual(webui._supervise_decision(3, 0.5, True), (False, 0.0))
        # 端口仍空着（stop/start 竞态）→ 退避重启
        self.assertEqual(webui._supervise_decision(3, 0.5, False), (True, 5.0))
        # 活得太短 = 崩溃循环 → 退避 5s；正常退出 → 立即拉起
        self.assertEqual(webui._supervise_decision(1, 0.5, False), (True, 5.0))
        self.assertEqual(webui._supervise_decision(0, 3600.0, False), (True, 1.0))

    def test_pidfile_roundtrip_and_garbage(self):
        self.assertIsNone(webui.read_studio_pid())
        webui.write_studio_pid(42, 8787)
        self.assertEqual(webui.read_studio_pid(), {"pid": 42, "port": 8787})
        webui.clear_studio_pid()
        self.assertIsNone(webui.read_studio_pid())
        Path(os.environ["FEIGE_STUDIO_PID_FILE"]).write_text("not json", encoding="utf-8")
        self.assertIsNone(webui.read_studio_pid())  # 脏 pidfile 当没配，不炸

    def test_start_when_port_already_alive(self):
        buf = io.StringIO()
        with mock.patch.object(webui, "port_alive", return_value=True), \
                mock.patch.object(webui, "read_studio_pid",
                                  return_value={"pid": 7, "port": 8787}), \
                mock.patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(buf):
            rc = webui.cmd_start(8787)
        self.assertEqual(rc, 0)
        self.assertIn("已在运行", buf.getvalue())
        popen.assert_not_called()  # 已在跑绝不再拉第二个守护

    def test_start_spawns_detached_supervisor_and_waits_for_port(self):
        buf = io.StringIO()
        with mock.patch.object(webui, "port_alive", side_effect=[False, True]), \
                mock.patch.object(webui, "read_studio_pid", return_value=None), \
                mock.patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(buf):
            rc = webui.cmd_start(8787)
        self.assertEqual(rc, 0)
        self.assertIn("已后台拉起", buf.getvalue())
        argv = popen.call_args[0][0]
        self.assertIn("supervise", argv)
        self.assertIn("8787", argv[-1])
        if os.name == "nt":
            self.assertTrue(popen.call_args[1]["creationflags"]
                            & subprocess.DETACHED_PROCESS)

    def test_start_restarts_living_supervisor_with_dead_port(self):
        buf = io.StringIO()
        webui.write_studio_pid(99, 8787)
        with mock.patch.object(webui, "port_alive", return_value=False), \
                mock.patch.object(webui, "pid_alive", return_value=True), \
                mock.patch.object(webui, "kill_tree") as kt, \
                mock.patch.object(webui, "time") as fake_time, \
                mock.patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(buf):
            popen.return_value.poll.return_value = None  # 永远起不来 → 走超时分支
            rc = webui.cmd_start(8787)
        self.assertEqual(rc, 1)
        kt.assert_called_once_with(99)  # 崩溃循环里的旧守护被整树杀掉重来
        fake_time.sleep.assert_called()  # 等待被 mock，测试不真等 10s
        self.assertIn("尚未就绪", buf.getvalue())

    def test_stop_kills_tree_and_clears_pidfile(self):
        buf = io.StringIO()
        webui.write_studio_pid(42, 8787)
        with mock.patch.object(webui, "pid_alive", return_value=True), \
                mock.patch.object(webui, "port_alive", return_value=False), \
                mock.patch.object(webui, "kill_tree") as kt, \
                contextlib.redirect_stdout(buf):
            rc = webui.cmd_stop()
        self.assertEqual(rc, 0)
        kt.assert_called_once_with(42)
        self.assertIn("已停止", buf.getvalue())
        self.assertIsNone(webui.read_studio_pid())

    def test_stop_without_pidfile_reports_front_process(self):
        webui.write_studio_pid(42, 8787)  # 守护死了但 pidfile 还在
        buf = io.StringIO()
        with mock.patch.object(webui, "pid_alive", return_value=False), \
                mock.patch.object(webui, "port_alive", return_value=True), \
                contextlib.redirect_stdout(buf):
            rc = webui.cmd_stop()
        self.assertEqual(rc, 1)
        self.assertIn("前台进程", buf.getvalue())
        self.assertIsNone(webui.read_studio_pid())  # 脏 pidfile 顺手清掉

    def test_status_states(self):
        cases = [
            (True, {"pid": 7, "port": 8787}, True, "运行中", 0),
            (True, None, False, "无守护", 0),
            (False, {"pid": 7, "port": 8787}, True, "崩溃循环", 1),
            (False, None, False, "未在运行", 1),
        ]
        for port_ok, info, alive, needle, rc in cases:
            buf = io.StringIO()
            with mock.patch.object(webui, "port_alive", return_value=port_ok), \
                    mock.patch.object(webui, "read_studio_pid", return_value=info), \
                    mock.patch.object(webui, "pid_alive", return_value=alive), \
                    contextlib.redirect_stdout(buf):
                self.assertEqual(webui.cmd_status(), rc)
            self.assertIn(needle, buf.getvalue())

    def test_supervise_worker_spawn_is_detached_and_exits_when_port_taken(self):
        # worker 也必须带脱离标志：无控制台的守护生出控制台程序会弹可见空窗口
        webui.write_studio_pid(111, 8787)
        buf = io.StringIO()
        with mock.patch.object(webui, "port_alive", return_value=True), \
                mock.patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(buf):
            popen.return_value.wait.return_value = 3  # worker 报端口被占
            rc = webui.cmd_supervise(8787)
        self.assertEqual(rc, 0)  # 端口已被别人接管 → 守护功成身退
        self.assertIsNone(webui.read_studio_pid())  # pidfile 顺手清掉
        kwargs = popen.call_args[1]
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
        self.assertEqual(kwargs["stdout"].name, str(webui.studio_log_file()))

    def test_main_dispatch_wiring(self):
        with mock.patch.object(sys, "argv", ["webui.py", "restart", "--port", "8787"]), \
                mock.patch.object(webui, "cmd_stop", return_value=0) as stop, \
                mock.patch.object(webui, "cmd_start", return_value=0) as start:
            self.assertEqual(webui.main(), 0)
        stop.assert_called_once()
        start.assert_called_once_with(8787)


if __name__ == "__main__":
    unittest.main()
