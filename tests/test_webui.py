"""Offline tests for WebUI pages, route editing, and HTTP guards."""

from __future__ import annotations

import http.client
import json
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

    def test_overview_shows_configuration_without_secret_values(self):
        marker = "never-render-this-secret"
        with mock.patch.object(webui.feige, "webhook_url", return_value=marker), \
                mock.patch.object(webui.feige, "_env", return_value=""), \
                mock.patch.object(webui.feige, "routes_file_path", return_value="routes.json"), \
                mock.patch.object(webui.feige, "load_routes", return_value=None):
            rendered = webui.page_overview().decode("utf-8")

        self.assertIn("feishu-webhook", rendered)
        self.assertNotIn(marker, rendered)

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
        with mock.patch.object(webui, "page_overview", return_value=b"overview"):
            status, content_type, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertEqual(body, "overview")

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


if __name__ == "__main__":
    unittest.main()
