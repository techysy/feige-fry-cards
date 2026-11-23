"""antigravity（agy）适配器回归测试（纯标准库 unittest，不发网络）。

fixture schema 已拿本机 agy 1.5.11 实证（`agy -p` + dump stdin Stop hook）：
payload 是 camelCase（conversationId/transcriptPath/modelName），transcript_full.jsonl
每行 `type` ∈ {USER_INPUT, PLANNER_RESPONSE, GENERIC, SYSTEM_MESSAGE}，PLANNER_RESPONSE
带 model / input_tokens / cache_read_tokens / output_tokens / thinking / tool_calls。

    python -m unittest tests.test_antigravity_adapter -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "adapters"))  # 适配器里的 `from common import …`

_spec = importlib.util.spec_from_file_location(
    "feige_antigravity", REPO / "adapters" / "antigravity" / "stop_notify.py")
agy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agy)

T0 = "2026-11-23T10:00:00Z"


def _ts(sec: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


def _planner(sec, model="gemini-3.8-flash-high", inp=0, cache=0, out=0,
             think="", tools=None, text=""):
    return json.dumps({"step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE",
                       "status": "DONE", "created_at": _ts(sec), "model": model,
                       "input_tokens": inp, "cache_read_tokens": cache,
                       "output_tokens": out, "thinking": think,
                       "tool_calls": tools, "content": text})


class TranscriptFixtureMixin:
    def write_transcript(self, dirpath: Path) -> Path:
        """两轮会话：turn1 有思考+工具，turn2（最后一轮）1 思考 1 工具 + 最终答复。"""
        p = Path(dirpath) / "transcript_full.jsonl"
        lines = [
            json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                        "status": "DONE", "created_at": _ts(0), "content": "第一问"}),
            _planner(2, inp=8771, cache=0, out=88, think="…thinking…",
                     tools=[{"name": "run_command", "args": {}}]),
            json.dumps({"step_index": 2, "source": "MODEL", "type": "GENERIC",
                        "status": "DONE", "created_at": _ts(3), "content": "Command exited"}),
            json.dumps({"step_index": 3, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                        "status": "DONE", "created_at": _ts(100), "content": "第二问"}),
            json.dumps({"step_index": 4, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
                        "status": "DONE", "created_at": _ts(101), "content": "system note"}),
            _planner(103, inp=2606, cache=7038, out=300, think="…thinking…",
                     tools=[{"name": "view_file", "args": {}}], text="最终答复"),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p


class SummarizeTranscriptTest(TranscriptFixtureMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.write_transcript(Path(self.tmp.name))
        self.summary = agy.summarize_transcript(str(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stats(self):
        s = self.summary
        # 🎫 只算最后一轮（turn1 输出 88 / turn2 输出 300）
        self.assertEqual(s["tokens"], 300)
        # 上下文水位 = 最后一条 PLANNER_RESPONSE 的 input + cache_read
        self.assertEqual(s["ctx"], 2606 + 7038)
        self.assertEqual(s["body"], "最终答复")

    def test_tools_and_thinking_are_last_turn_only(self):
        # turn1 1 tool + 1 think，turn2 1 tool + 1 think，卡片只报最后一轮
        self.assertEqual(self.summary["tools"], 1)
        self.assertEqual(self.summary["thinking"], 1)

    def test_session_scope_uses_whole_file(self):
        with mock.patch.dict(os.environ, {"FEIGE_STATS_SCOPE": "session"}):
            self.setUp()
            s = self.summary
        self.assertEqual((s["tools"], s["thinking"], s["tokens"]), (2, 2, 388))
        self.assertEqual(s["elapsed"], "1m 43s")  # 全文件跨度 103s

    def test_no_user_input_falls_back_to_whole_file(self):
        # 没有提问行（如 hook 触发时转录只有半轮）：退化为全文件统计
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "transcript_full.jsonl"
            p.write_text("\n".join([
                _planner(0, inp=10, cache=0, out=40, think="…",
                         tools=[{"name": "run_command", "args": {}}], text="答"),
                _planner(1, inp=10, cache=0, out=2, text="答"),
            ]) + "\n", encoding="utf-8")
            s = agy.summarize_transcript(str(p))
            self.assertEqual((s["tools"], s["thinking"], s["tokens"]), (1, 1, 42))
            self.assertEqual(s["body"], "答")

    def test_last_turn_without_text_does_not_fall_back(self):
        # 最后一轮没文本就发占位符，绝不回落上一轮旧文（对齐 Claude 适配器口径）
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "transcript_full.jsonl"
            p.write_text("\n".join([
                _planner(0, text="上一轮的答复"),
                json.dumps({"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                            "status": "DONE", "created_at": _ts(10), "content": "下一问"}),
                _planner(11, out=5),
            ]) + "\n", encoding="utf-8")
            s = agy.summarize_transcript(str(p))
            self.assertEqual(s["body"], "……（本轮无文本输出）")

    def test_model_prefix_tool_name(self):
        self.assertEqual(self.summary["model"], "antigravity · gemini-3.8-flash-high")
        self.assertEqual(self.summary["raw_model"], "gemini-3.8-flash-high")

    def test_elapsed_is_last_turn_only(self):
        # 最后一轮 100s → 103s = 3s，而不是全程 103s
        self.assertEqual(self.summary["elapsed"], "3.0s")

    def test_dirty_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "transcript_full.jsonl"
            p.write_text("not json\n\n" + _planner(0, text="ok") + "\n", encoding="utf-8")
            s = agy.summarize_transcript(str(p))
            self.assertEqual(s["body"], "ok")

    def test_no_planner_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "transcript_full.jsonl"
            p.write_text(json.dumps({"type": "USER_INPUT", "created_at": _ts(0),
                                     "content": "hi"}) + "\n", encoding="utf-8")
            self.assertIsNone(agy.summarize_transcript(str(p)))

    def test_missing_file_returns_none(self):
        self.assertIsNone(agy.summarize_transcript("/nonexistent/x.jsonl"))


class FmtContextTest(unittest.TestCase):
    def test_without_limit_falls_back_to_absolute(self):
        self.assertEqual(agy.fmt_context(153_600, 0), "153.6k")

    def test_empty(self):
        self.assertEqual(agy.fmt_context(0, 1_048_576), "")

    def test_percentage_rounding(self):
        self.assertEqual(agy.fmt_context(524_288, 1_048_576), "524.3k/1.0m (50%)")


class ContextWindowTest(unittest.TestCase):
    def setUp(self):
        # 隔离本机 CLAUDE_CODE_MAX_CONTEXT_TOKENS（真实环境变量会盖过已知表）
        for var in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "FEIGE_CONTEXT_TOTAL"):
            self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
            os.environ.pop(var, None)

    def test_known_models(self):
        self.assertEqual(agy.context_window("gemini-3.8-flash-high"), 1_048_576)
        self.assertEqual(agy.context_window("claude-sonnet-4-6"), 200_000)
        self.assertEqual(agy.context_window("gpt-oss-120b-medium"), 131_072)

    def test_unknown_model(self):
        self.assertEqual(agy.context_window("mystery-1"), 0)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"FEIGE_CONTEXT_TOTAL": "123456"}):
            self.assertEqual(agy.context_window("mystery-1"), 123_456)


class MainWiringTest(TranscriptFixtureMixin, unittest.TestCase):
    def test_main_passes_transcript_to_send_card(self):
        """send_card 必须收到 transcript=路径：后台发送进程开卡前重读的接线。"""
        with tempfile.TemporaryDirectory() as d:
            path = self.write_transcript(Path(d))
            with mock.patch.dict(os.environ, {"FEIGE_HOOK_NOTIFY": "1"}):
                with mock.patch.object(agy, "send_card") as sc:
                    agy.main({"conversationId": "conv-1", "cwd": "/x/demo",
                              "transcriptPath": str(path)})
        kwargs = sc.call_args.kwargs
        self.assertEqual(kwargs.get("transcript"), str(path))
        self.assertEqual(kwargs.get("session"), "conv-1")
        self.assertEqual(kwargs.get("agent"), "Antigravity")

    def test_main_snake_case_fallback(self):
        """snake_case transcript_path 也认（forward-compat / 手动调用）。"""
        with tempfile.TemporaryDirectory() as d:
            path = self.write_transcript(Path(d))
            with mock.patch.dict(os.environ, {"FEIGE_HOOK_NOTIFY": "1"}):
                with mock.patch.object(agy, "send_card") as sc:
                    agy.main({"conversationId": "conv-1", "cwd": "/x/demo",
                              "transcript_path": str(path)})
        self.assertEqual(sc.call_args.kwargs.get("transcript"), str(path))

    def test_missing_transcript_no_card(self):
        with mock.patch.dict(os.environ, {"FEIGE_HOOK_NOTIFY": "1"}):
            with mock.patch.object(agy, "send_card") as sc:
                agy.main({"conversationId": "conv-1", "cwd": "/x/demo"})
        sc.assert_not_called()


class GateTest(unittest.TestCase):
    def test_default_off_explicit_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEIGE_HOOK_NOTIFY", None)
            # 默认关：main 不经 run() 直调时靠 run() 判定，这里只测环境开关本身
            self.assertFalse((os.environ.get("FEIGE_HOOK_NOTIFY") or "").lower() in ("1", "true", "yes"))
        with mock.patch.dict(os.environ, {"FEIGE_HOOK_NOTIFY": "1"}):
            from common import hook_enabled
            self.assertTrue(hook_enabled())

    def test_stop_dispatch_detects_antigravity(self):
        spec = importlib.util.spec_from_file_location("feige_stop", REPO / "hooks" / "stop.py")
        stop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stop)
        self.assertEqual(stop.detect_host({"conversationId": "c1"}, env={}), "antigravity")
        self.assertEqual(stop.detect_host(
            {"transcript_path": "C:/x/.gemini/antigravity-cli/brain/a/logs/t.jsonl"},
            env={}), "antigravity")
        # 已有宿主识别不受影响
        self.assertEqual(stop.detect_host({"client_type": "kimi_code_cli"}, env={}), "kimi-code")
        self.assertEqual(stop.detect_host({"turn_id": "t1"}, env={}), "codex")
        self.assertEqual(stop.detect_host({"transcript_path": "/x/.claude/t.jsonl"}, env={}), "claude-code")


if __name__ == "__main__":
    unittest.main()
