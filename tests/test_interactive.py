"""交互式卡片、折叠面板、厂商 Logo、长连接与无头续跑离线测试（零外网请求）。

    python -m unittest tests.test_interactive -v
"""

from __future__ import annotations

import http.client
import importlib.util
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
import feige_interactive  # noqa: E402
import webui  # noqa: E402


class VendorLogoAndFamilyCardTest(unittest.TestCase):
    def test_vendor_logo_and_official_name_mapping(self):
        cases = [
            ("tencent/hy4-flash:free", "hunyuan.png", "Hy4 flash"),
            ("claude-code · claude-sonnet-4", "claude.png", "Claude sonnet-4"),
            ("deepseek-v4.1-flash", "deepseek.png", "DeepSeek v4.1-flash"),
            ("kimi-code · kimi-k3", "kimi.png", "Kimi k3"),
            ("longcat/LongCat-2.5-Preview", "longcat.png", "LongCat 2.5-Preview"),
            ("unknown/custom-model", None, None),
        ]
        for model, exp_file, exp_name in cases:
            with self.subTest(model=model):
                self.assertEqual(feige.resolve_vendor_logo_asset(model), exp_file)
                self.assertEqual(feige.resolve_vendor_display_name(model), exp_name)
                if exp_file:
                    self.assertTrue((feige._ASSETS_VENDOR_DIR / exp_file).is_file())

    def test_collapsible_panel_and_logo_in_card(self):
        card = feige.build_card(
            "任务完成",
            "精简摘要",
            stats={"project": "feige", "model": "claude-code · claude-sonnet-4",
                   "thinking": 2, "tools": 3, "context": "150.0k/1.0m (15%)",
                   "tokens": 1200, "elapsed": "12.0s"},
            status="ok",
            detail_body="完整详细报告正文内容",
            tool_summary="Read ×2 · Edit",
            host="claude-code",
            ctx_id="ctx1234567890abcdef",
            interactive=True,
            resumable=True,
            logo_image_key="img_v3_mock_logo",
        )
        self.assertEqual(card["config"]["locales"], ["zh_cn", "en_us"])
        els = card["body"]["elements"]
        panels = [e for e in els if e.get("tag") == "collapsible_panel"]
        self.assertEqual(len(panels), 1)
        panel = panels[0]
        self.assertEqual(panel["element_id"], feige.UNIFIED_PANEL_ELEMENT_ID)
        self.assertFalse(panel["expanded"])
        self.assertEqual(panel["border"]["color"], "green")
        self.assertEqual(panel["header"]["icon"]["img_key"], "img_v3_mock_logo")
        self.assertEqual(panel["header"]["icon_position"], "left")
        self.assertIn("Claude sonnet-4", panel["header"]["title"]["content"])
        self.assertIn("[█▓░░░░░░] 15%", panel["header"]["title"]["content"])
        panel_dump = json.dumps(panel, ensure_ascii=False)
        self.assertIn("完整详细报告正文内容", panel_dump)
        self.assertIn("Read ×2 · Edit", panel_dump)
        # 交互区包含追问表单 + 防误触快捷按钮
        forms = [e for e in els if e.get("tag") == "form"]
        self.assertEqual(len(forms), 1)
        cols = [e for e in els if e.get("tag") == "column_set"]
        self.assertEqual(len(cols), 1)
        btn_dump = json.dumps(cols[0], ensure_ascii=False)
        self.assertIn("confirm", btn_dump)
        self.assertIn("ctx1234567890abcdef", btn_dump)

    def test_fallback_fries_icon_without_logo_key(self):
        card = feige.build_card(
            "完成", "摘要",
            stats={"model": "unknown-m", "tools": 1},
            status="error",
        )
        panel = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"][0]
        self.assertEqual(panel["border"]["color"], "red")
        self.assertEqual(panel["header"]["icon"]["tag"], "standard_icon")
        self.assertEqual(panel["header"]["icon_position"], "right")
        self.assertNotIn("🍟", panel["header"]["title"]["content"])
        self.assertTrue(panel["header"]["title"]["content"].startswith("unknown-m"))

    def test_unsupported_host_card_shows_clear_notice_instead_of_form(self):
        card = feige.build_card(
            "完成", "摘要",
            stats={"model": "antigravity · gemini-3.8-flash-high"},
            host="antigravity",
            ctx_id="ctx_agy_1",
            interactive=True,
            resumable=False,
        )
        els = card["body"]["elements"]
        self.assertEqual([e for e in els if e.get("tag") == "form"], [])
        dump = json.dumps(els, ensure_ascii=False)
        self.assertIn("暂不支持远程无头续跑", dump)
        self.assertIn("unsupported", dump)

    def test_webhook_channel_has_panel_but_no_interactive_buttons(self):
        ok, out = feige.send_report(
            "t", "摘要",
            stats={"project": "p", "model": "claude-sonnet-4", "tools": 2},
            channel="feishu-webhook", dry_run=True,
            host="claude-code", session_id="s1", cwd="C:/secret/cwd",
            detail_body="详细正文",
        )
        self.assertTrue(ok)
        payload = json.loads(out)
        els = payload["card"]["body"]["elements"]
        self.assertTrue(any(e.get("tag") == "collapsible_panel" for e in els))
        self.assertFalse(any(e.get("tag") == "form" for e in els))
        self.assertNotIn("C:/secret/cwd", out)
        self.assertNotIn("s1", out)


class SessionIsolationAndLogoUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SESSIONS_FILE"] = str(Path(self.tmp.name, "sessions.json"))
        env["FEIGE_LOGO_CACHE_FILE"] = str(Path(self.tmp.name, "logos.json"))
        env["FEIGE_SETTINGS_FILE"] = str(Path(self.tmp.name, "settings.json"))
        self.patch = mock.patch.dict(os.environ, env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        feige._LOGO_CACHE.clear()

    def test_cardkit_send_never_exposes_cwd_or_session_id_on_card(self):
        os.environ["FEISHU_APP_ID"] = "cli_test"
        os.environ["FEISHU_APP_SECRET"] = "sec_test"
        os.environ["FEISHU_NOTIFY_CHAT_ID"] = "oc_chat1"
        captured_cards = []

        def fake_api(method, path, body=None):
            if path == "/cardkit/v1/cards":
                captured_cards.append(json.loads(body["data"]))
                return {"code": 0, "data": {"card_id": "card_abc"}}
            if path.startswith("/im/v1/messages"):
                return {"code": 0, "data": {"message_id": "om_msg1"}}
            return {"code": 0, "data": {}}

        with mock.patch.object(feige, "upload_image_bytes", return_value="img_claude_k") as up, \
                mock.patch.object(feige, "call_app_api", side_effect=fake_api):
            ok, detail = feige.send_report(
                "完成", "摘要",
                stats={"project": "demo", "model": "claude-code · claude-sonnet-4", "tools": 1},
                channel="feishu-cardkit",
                host="claude-code",
                session_id="secret-native-session-999",
                cwd="C:\\Users\\Private\\SecretRepo",
                detail_body="详细正文",
            )
        self.assertTrue(ok, detail)
        up.assert_called_once()
        self.assertEqual(len(captured_cards), 1)
        card_str = json.dumps(captured_cards[0], ensure_ascii=False)
        self.assertIn("img_claude_k", card_str)
        self.assertNotIn("SecretRepo", card_str)
        self.assertNotIn("secret-native-session-999", card_str)
        # 第二次发送同一家族模型应命中本地缓存，不重复上传
        with mock.patch.object(feige, "upload_image_bytes") as up2, \
                mock.patch.object(feige, "call_app_api", side_effect=fake_api):
            ok2, _ = feige.send_report(
                "完成2", "摘要2",
                stats={"model": "claude-opus-4"},
                channel="feishu-cardkit",
            )
        self.assertTrue(ok2)
        up2.assert_not_called()

        # 验证本地会话映射正确保留了敏感信息且 list_sessions 脱敏
        listed = feige_interactive.list_sessions()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("cwd", listed[0])
        self.assertNotIn("session_id", listed[0])
        rec = feige_interactive.get_session(listed[0]["ctx_id"])
        self.assertEqual(rec["cwd"], "C:\\Users\\Private\\SecretRepo")
        self.assertEqual(rec["session_id"], "secret-native-session-999")
        self.assertEqual(rec["card_id"], "card_abc")
        self.assertEqual(rec["message_id"], "om_msg1")


class InteractiveCallbackAndResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name, "repo")
        self.workdir.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SESSIONS_FILE"] = str(Path(self.tmp.name, "sessions.json"))
        env["FEIGE_SETTINGS_FILE"] = str(Path(self.tmp.name, "settings.json"))
        env["FEIGE_LOGO_CACHE_FILE"] = str(Path(self.tmp.name, "logos.json"))
        self.patch = mock.patch.dict(os.environ, env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _make_ctx(self, host="claude-code", session_id="sess_1", chat_id="oc_room"):
        return feige_interactive.register_session(
            host=host,
            session_id=session_id,
            cwd=str(self.workdir),
            project="demo",
            model="claude-code · claude-sonnet-4",
            title="demo 任务完成",
            chat_id=chat_id,
            card_id="card_1",
            message_id="om_1",
            last_body="上一轮摘要",
        )

    @staticmethod
    def _event(ctx_id, action="followup", prompt="", open_id="ou_user1", user_id="", chat_id="oc_room"):
        return {
            "event": {
                "operator": {"open_id": open_id, "user_id": user_id},
                "context": {"open_chat_id": chat_id, "open_message_id": "om_1"},
                "action": {
                    "value": {"feige_action": action, "ctx_id": ctx_id},
                    "form_value": {"followup_prompt": prompt} if prompt is not None else {},
                },
            }
        }

    def test_operator_allowlist_and_schema2_user_id_fallback(self):
        ctx_id = self._make_ctx()
        os.environ["FEIGE_ALLOW_USERS"] = "ou_admin, uid_fallback"
        with mock.patch.object(feige_interactive, "resolve_cli_executable", return_value="claude"):
            denied = feige_interactive.handle_card_action(
                self._event(ctx_id, action="continue", open_id="ou_stranger"),
                spawn_bg=False,
            )
            self.assertEqual(denied["toast"]["type"], "error")
            self.assertIn("白名单", denied["toast"]["content"])
            self.assertNotIn("card", denied)

            # Schema 2 无 open_id，回落 user_id
            allowed = feige_interactive.handle_card_action(
                self._event(ctx_id, action="continue", open_id="", user_id="uid_fallback"),
                spawn_bg=False,
            )
            self.assertEqual(allowed["toast"]["type"], "info")
            self.assertEqual(allowed["card"]["data"]["header"]["template"], "blue")

    def test_chat_mismatch_rejected(self):
        ctx_id = self._make_ctx(chat_id="oc_room_a")
        res = feige_interactive.handle_card_action(
            self._event(ctx_id, action="continue", chat_id="oc_room_b"),
            spawn_bg=False,
        )
        self.assertEqual(res["toast"]["type"], "error")
        self.assertIn("不匹配", res["toast"]["content"])

    def test_empty_followup_warns_without_mutating_state(self):
        ctx_id = self._make_ctx()
        with mock.patch.object(feige_interactive, "resolve_cli_executable", return_value="claude"):
            res = feige_interactive.handle_card_action(
                self._event(ctx_id, action="followup", prompt="   "),
                spawn_bg=False,
            )
        self.assertEqual(res["toast"]["type"], "warning")
        self.assertNotIn("card", res)
        self.assertEqual(feige_interactive.get_session(ctx_id)["status"], "idle")

    def test_unsupported_host_and_concurrent_lock(self):
        agy_ctx = self._make_ctx(host="antigravity", session_id="conv_1")
        res = feige_interactive.handle_card_action(
            self._event(agy_ctx, action="unsupported"),
            spawn_bg=False,
        )
        self.assertIn("暂不支持", res["toast"]["content"])

        cc_ctx = self._make_ctx(host="claude-code", session_id="sess_busy")
        with mock.patch.object(feige_interactive, "resolve_cli_executable", return_value="claude"):
            first = feige_interactive.handle_card_action(
                self._event(cc_ctx, action="followup", prompt="补充测试"),
                spawn_bg=False,
            )
            self.assertIn("card", first)
            second = feige_interactive.handle_card_action(
                self._event(cc_ctx, action="continue"),
                spawn_bg=False,
            )
            self.assertEqual(second["toast"]["type"], "info")
            self.assertIn("正在执行中", second["toast"]["content"])

    def test_headless_resume_commands_and_worker_card_update_and_fallback(self):
        ctx_id = self._make_ctx(host="claude-code", session_id="sess_cc")

        def fake_runner(argv, **kwargs):
            self.assertEqual(kwargs["env"]["FEIGE_RESUME_CHILD"], "1")
            self.assertEqual(kwargs["env"]["FEIGE_HOOK_NOTIFY"], "0")
            self.assertIn("--resume", argv)
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=json.dumps({"result": "已补充测试并全部通过", "session_id": "sess_cc_2",
                                   "duration_ms": 2300, "usage": {"output_tokens": 450}}),
                stderr="",
            )

        with mock.patch.object(feige_interactive, "resolve_cli_executable", return_value="claude"), \
                mock.patch.object(feige, "update_card", return_value=2) as upd, \
                mock.patch.object(feige, "resolve_vendor_logo_image_key", return_value="img_cc"):
            feige_interactive._execute_resume_and_update(ctx_id, "请补充单测", runner=fake_runner)

        upd.assert_called_once()
        updated_card = upd.call_args[0][1]
        self.assertEqual(updated_card["header"]["template"], "green")
        dump = json.dumps(updated_card, ensure_ascii=False)
        self.assertIn("已补充测试并全部通过", dump)
        self.assertIn("img_cc", dump)
        rec = feige_interactive.get_session(ctx_id)
        self.assertEqual(rec["status"], "idle")
        self.assertEqual(rec["session_id"], "sess_cc_2")
        self.assertEqual(rec["card_sequence"], 2)

        # 当 update_card 失败（如卡片超 24h 过期）时，自动退化为回复新卡
        with mock.patch.object(feige_interactive, "resolve_cli_executable", return_value="claude"), \
                mock.patch.object(feige, "update_card", side_effect=RuntimeError("expired")), \
                mock.patch.object(feige, "send_feishu_cardkit_detail",
                                  return_value={"card_id": "card_new", "message_id": "om_new", "detail": "sent"}) as send_new, \
                mock.patch.object(feige, "resolve_vendor_logo_image_key", return_value=""):
            feige_interactive._execute_resume_and_update(ctx_id, "再跑一遍", runner=fake_runner)
        send_new.assert_called_once()
        self.assertEqual(send_new.call_args.kwargs["reply_to_message_id"], "om_1")
        self.assertEqual(feige_interactive.get_session(ctx_id)["card_id"], "card_new")

    def test_codex_zcode_kimi_command_shapes(self):
        with mock.patch.object(feige_interactive, "resolve_cli_executable", side_effect=lambda c: f"/bin/{c}"):
            cmd_x, err_x = feige_interactive.build_resume_command("codex", "th_1", "fix", codex_out_file="o.txt")
            self.assertIsNone(err_x)
            self.assertEqual(cmd_x[:4], ["/bin/codex", "exec", "resume", "th_1"])

            cmd_z, err_z = feige_interactive.build_resume_command("zcode", "sess_z", "go", cwd=str(self.workdir))
            self.assertIsNone(err_z)
            self.assertIn("--resume", cmd_z)
            self.assertIn("--cwd", cmd_z)

            cmd_k, err_k = feige_interactive.build_resume_command("kimi-code", "sess_k", "check")
            self.assertIsNone(err_k)
            self.assertEqual(cmd_k, ["/bin/kimi", "-p", "check", "-S", "sess_k"])

    def test_resume_child_suppresses_stop_hooks(self):
        stop = importlib.util.spec_from_file_location("feige_stop_t", REPO / "hooks" / "stop.py")
        stop_mod = importlib.util.module_from_spec(stop)
        stop.loader.exec_module(stop_mod)

        sys.path.insert(0, str(REPO / "adapters"))
        import common
        called = []
        with mock.patch.dict(os.environ, {"FEIGE_RESUME_CHILD": "1", "FEIGE_HOOK_NOTIFY": "1"}):
            common.run(lambda: called.append(1))
            with mock.patch("sys.stdin") as si:
                stop_mod.main()
                si.read.assert_not_called()
        self.assertEqual(called, [])


class WSDegradationAndStudioIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FEIGE_", "FEISHU_", "LARK_"))}
        env["FEIGE_SETTINGS_FILE"] = str(Path(self.tmp.name, "settings.json"))
        env["FEIGE_SESSIONS_FILE"] = str(Path(self.tmp.name, "sessions.json"))
        self.patch = mock.patch.dict(os.environ, env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_ws_manager_graceful_degradation_when_lark_oapi_missing(self):
        mgr = feige_interactive.FeishuWSManager()
        os.environ["FEIGE_WS_ENABLED"] = "1"
        os.environ["FEISHU_APP_ID"] = "cli_x"
        os.environ["FEISHU_APP_SECRET"] = "sec_x"
        with mock.patch.object(feige_interactive, "lark_oapi_available", return_value=False):
            st = mgr.sync_with_config()
        self.assertFalse(st["running"])
        self.assertEqual(st["state"], "unavailable")
        self.assertIn("lark-oapi", st["error"])

    def test_studio_feishu_settings_interactive_toggles_and_allowlist(self):
        body = urllib.parse.urlencode({
            "form": "feishu",
            "feishu_app_id": "cli_999",
            "feishu_app_secret": "sec_hidden_999",
            "feishu_interactive": "1",
            "feishu_ws_enabled": "1",
            "vendor_logo_enabled": "1",
            "panel_expanded": "1",
            "interactive_allow_users": "ou_1, ou_2",
            "resume_timeout_seconds": "180",
        }).encode("utf-8")
        resp = webui.handle_settings_post(body).decode("utf-8")
        self.assertIn("飞书渠道已保存", resp)
        self.assertNotIn("sec_hidden_999", resp)  # 密钥永不回显
        saved = json.loads(Path(os.environ["FEIGE_SETTINGS_FILE"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["feishu_interactive"], "1")
        self.assertEqual(saved["feishu_ws_enabled"], "1")
        self.assertEqual(saved["panel_expanded"], "1")
        self.assertEqual(saved["interactive_allow_users"], "ou_1, ou_2")
        self.assertEqual(saved["resume_timeout_seconds"], "180")
        self.assertEqual(feige.allow_users_list(), ["ou_1", "ou_2"])
        self.assertTrue(feige.ws_enabled())
        self.assertTrue(feige.panel_expanded_default())

        # 无效超时拒绝写入
        bad = webui.handle_settings_post(b"form=feishu&resume_timeout_seconds=5").decode("utf-8")
        self.assertIn("非法续跑超时秒数", bad)

    def test_studio_serves_vendor_logos_and_blocks_traversal(self):
        server = webui.Server(("127.0.0.1", 0), webui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=3)))
        port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/assets/vendor-logos/claude.png")
        r1 = conn.getresponse()
        b1 = r1.read()
        self.assertEqual(r1.status, 200)
        self.assertEqual(r1.getheader("Content-Type"), "image/png")
        self.assertTrue(b1.startswith(b"\x89PNG"))
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/assets/vendor-logos/../../feige.py")
        r2 = conn.getresponse()
        r2.read()
        self.assertEqual(r2.status, 404)
        conn.close()

    def test_preview_renders_collapsible_panel_logo_and_interactive_section(self):
        q = urllib.parse.parse_qs(urllib.parse.urlencode({
            "go": "1",
            "title": "重构完毕",
            "channel": "feishu-cardkit",
            "host": "claude-code",
            "model": "claude-sonnet-4",
            "body": "精简摘要",
            "detail": "详细展开内容",
            "tool_summary": "Read ×3",
        }))
        html_out = webui.page_preview(q).decode("utf-8")
        self.assertIn("pv-panel", html_out)
        self.assertIn("/assets/vendor-logos/claude.png", html_out)
        self.assertIn("Claude sonnet-4", html_out)
        self.assertIn("详细展开内容", html_out)
        self.assertIn("发送追问", html_out)
        self.assertIn("collapsible_panel", html_out)

    def test_studio_fold_cards_and_status_badges_and_version_2_0(self):
        self.assertEqual(feige.__version__, "2.1.0")
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "2.1.0"', pyproject)

        studio_html = webui.page_studio().decode("utf-8")
        self.assertIn("fold-card", studio_html)
        self.assertIn("fold-badge", studio_html)
        self.assertIn("hint-fold", studio_html)
        self.assertIn("保存并预览", studio_html)

        status_html = webui.page_status().decode("utf-8")
        self.assertIn("badge ", status_html)

        pv_html = webui.page_preview({}).decode("utf-8")
        self.assertIn("pv-stage", pv_html)
        self.assertIn("pv-model-presets", pv_html)
        self.assertIn("/api/preview", pv_html)

    def test_api_preview_and_save_redirect_and_csrf_protection(self):
        server = webui.Server(("127.0.0.1", 0), webui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=3)))
        port = server.server_address[1]

        # 1) 正常调用 POST /api/preview 返回真实 CardKit v2.0 卡片与厂商资产元数据
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        req_body = json.dumps({
            "title": "测试卡片",
            "channel": "feishu-cardkit",
            "host": "claude-code",
            "status": "ok",
            "model": "claude-sonnet-4",
            "thinking": "not-a-number",
            "tools": "4",
            "body": "**完成**",
            "detail": "详细内容",
        }).encode("utf-8")
        conn.request("POST", "/api/preview", body=req_body,
                     headers={"Content-Type": "application/json",
                              "Origin": f"http://127.0.0.1:{port}"})
        r1 = conn.getresponse()
        d1 = json.loads(r1.read().decode("utf-8"))
        conn.close()
        self.assertEqual(r1.status, 200)
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["logo_url"], "/assets/vendor-logos/claude.png")
        self.assertEqual(d1["official_model"], "Claude sonnet-4")
        self.assertTrue(d1["resumable"])
        self.assertEqual(d1["card"]["schema"], "2.0")

        # 2) 跨站 Origin 必须 403
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/preview", body=b"{}",
                     headers={"Origin": "http://evil.example"})
        r2 = conn.getresponse()
        r2.read()
        conn.close()
        self.assertEqual(r2.status, 403)

        # 3) POST /settings 带 _next=preview 返回 303 到 /preview?saved=1
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        form_body = urllib.parse.urlencode({
            "form": "feishu",
            "feishu_interactive": "1",
            "_next": "preview",
        }).encode("utf-8")
        conn.request("POST", "/settings", body=form_body,
                     headers={"Content-Type": "application/x-www-form-urlencoded",
                              "Origin": f"http://127.0.0.1:{port}"})
        r3 = conn.getresponse()
        r3.read()
        conn.close()
        self.assertEqual(r3.status, 303)
        self.assertEqual(r3.getheader("Location"), "/preview?saved=1")

    def test_split_interactive_card_env_lock_and_no_fries_emoji(self):
        # 1) 独立保存 interactive 卡与 feishu 凭据卡互不误清
        webui.handle_settings_post(urllib.parse.urlencode({
            "form": "interactive",
            "feishu_interactive": "1",
            "feishu_ws_enabled": "1",
            "interactive_allow_users": "ou_keep",
        }).encode("utf-8"))
        # 再单独保存 feishu 凭据卡（不带交互字段），不得清掉上面的开关与白名单
        webui.handle_settings_post(urllib.parse.urlencode({
            "form": "feishu",
            "feishu_app_id": "cli_new",
        }).encode("utf-8"))
        saved = json.loads(Path(os.environ["FEIGE_SETTINGS_FILE"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["feishu_interactive"], "1")
        self.assertEqual(saved["feishu_ws_enabled"], "1")
        self.assertEqual(saved["interactive_allow_users"], "ou_keep")
        self.assertEqual(saved["feishu_app_id"], "cli_new")

        # 2) 被环境变量接管的键在 Studio 页面标为锁定且预览不包含 🍟
        with mock.patch.dict(os.environ, {"FEISHU_APP_ID": "cli_env_locked"}):
            studio_html = webui.page_studio().decode("utf-8")
            self.assertIn("飞书渠道 · 凭据", studio_html)
            self.assertIn("交互与长连接", studio_html)
            self.assertIn("env-locked", studio_html)
            self.assertIn("env-note", studio_html)
            self.assertIn("check-top", studio_html)
        pv_html = webui.page_preview(urllib.parse.parse_qs("go=1&channel=feishu-webhook")).decode("utf-8")
        self.assertNotIn("🍟", pv_html)

    def test_adapter_cache_inspection_and_sync(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            old_cache = home / ".codex" / "plugins" / "cache" / "feige-fry-cards" / "feige-fry-cards" / "1.0.1"
            old_cache.mkdir(parents=True)
            (old_cache / "feige.py").write_text('__version__ = "1.0.1"\n', encoding="utf-8")

            c_dir = home / ".claude"
            c_dir.mkdir()
            (c_dir / "settings.json").write_text(json.dumps({
                "hooks": {"Stop": [{"hooks": [{"type": "command",
                    "command": f'python "{old_cache.as_posix()}/hooks/stop.py"'}]}]}
            }), encoding="utf-8")

            z_mkt = home / ".zcode" / "cli" / "plugins" / "marketplaces" / "feige-fry-cards"
            z_mkt.mkdir(parents=True)
            (z_mkt / "feige.py").write_text('__version__ = "1.0.1"\n', encoding="utf-8")
            z_inst = home / ".zcode" / "cli" / "plugins" / "installed_plugins.json"
            z_inst.write_text(json.dumps({"version": 1, "plugins": [
                {"name": "feige-fry-cards", "version": "1.0.1", "installPath": str(old_cache)}
            ]}), encoding="utf-8")

            before = webui.inspect_adapter_caches(home)
            self.assertTrue(any(f["stale"] for f in before))
            html_before = webui.page_adapters(home).decode("utf-8")
            self.assertIn("待同步", html_before)

            updated, errs = webui.sync_adapters_to_latest(home)
            self.assertEqual(errs, [])
            self.assertTrue(updated)
            after = webui.inspect_adapter_caches(home)
            self.assertFalse(any(f["stale"] for f in after))
            c_doc = json.loads((c_dir / "settings.json").read_text(encoding="utf-8"))
            cmd = c_doc["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertIn(str(REPO / "hooks" / "stop.py").replace("\\", "/"), cmd)
            z_doc = json.loads(z_inst.read_text(encoding="utf-8"))
            self.assertEqual(z_doc["plugins"][0]["version"], feige.__version__)


if __name__ == "__main__":
    unittest.main()
