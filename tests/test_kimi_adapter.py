"""kimi-code 适配器回归测试（纯标准库 unittest，不发网络）。

    python -m unittest tests.test_kimi_adapter -v
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
    "feige_kimi_code", REPO / "adapters" / "kimi-code" / "stop_notify.py")
kimi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kimi)

T0 = 1_790_000_000_000  # ms


def _loop(t, turn, etype, **kw):
    event = {"type": etype, "turnId": turn}
    event.update(kw)
    return json.dumps({"type": "context.append_loop_event", "agentId": "main",
                       "time": t, "event": event})


def _usage(t, inputs, output):
    return json.dumps({"type": "usage.record", "agentId": "main", "time": t,
                       "usage": {"inputOther": inputs[0], "inputCacheRead": inputs[1],
                                 "inputCacheCreation": inputs[2], "output": output},
                       "usageScope": "turn"})


class WireFixtureMixin:
    def write_wire(self, dirpath: Path) -> Path:
        home = Path(dirpath)
        wire = home / "sessions" / "wd_x" / "session_abc" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        lines = [
            json.dumps({"type": "metadata", "time": T0}),
            # turn 1
            _loop(T0 + 1000, "1", "tool.call", name="Bash"),
            _loop(T0 + 2000, "1", "content.part", part={"type": "think", "think": "…"}),
            _usage(T0 + 3000, (1000, 152_600, 0), 500),
            json.dumps({"type": "llm.request", "model": "kimi-k3", "time": T0 + 4000}),
            # turn 2（最后一轮）
            _loop(T0 + 101_000, "2", "content.part", part={"type": "think", "think": "…"}),
            _loop(T0 + 102_000, "2", "tool.call", name="Read"),
            _loop(T0 + 103_000, "2", "content.part", part={"type": "text", "text": "最终答复"}),
            _usage(T0 + 104_500, (1200, 152_400, 0), 300),
        ]
        wire.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return home


class SummarizeWireTest(WireFixtureMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.write_wire(Path(self.tmp.name))
        self.summary = kimi.summarize_wire(kimi.find_wire("session_abc", self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stats(self):
        s = self.summary
        # 🎫 同样只算最后一轮（turn1 输出 500 / turn2 输出 300）
        self.assertEqual(s["tokens"], 300)
        self.assertEqual(s["ctx"], 153_600)
        self.assertEqual(s["body"], "最终答复")

    def test_tools_and_thinking_are_last_turn_only(self):
        # 两轮各有 1 tool + 1 think，卡片只报最后一轮
        self.assertEqual(self.summary["tools"], 1)
        self.assertEqual(self.summary["thinking"], 1)

    def test_no_turnid_falls_back_to_whole_file(self):
        # 无 turnId 的老格式：💭/🔧/🎫 退化为全文件统计（全部计入）
        with tempfile.TemporaryDirectory() as d:
            wire = Path(d) / "wire.jsonl"
            wire.write_text("\n".join([
                json.dumps({"type": "context.append_loop_event", "agentId": "main",
                            "time": T0, "event": {"type": "tool.call", "name": "Bash"}}),
                json.dumps({"type": "context.append_loop_event", "agentId": "main",
                            "time": T0 + 1, "event": {"type": "tool.call", "name": "Read"}}),
                json.dumps({"type": "context.append_loop_event", "agentId": "main",
                            "time": T0 + 2, "event": {"type": "content.part",
                                                      "part": {"type": "think", "think": "…"}}}),
                _usage(T0 + 3, (10, 0, 0), 40),
                _usage(T0 + 4, (10, 0, 0), 2),
            ]) + "\n", encoding="utf-8")
            s = kimi.summarize_wire(wire)
            self.assertEqual((s["tools"], s["thinking"], s["tokens"]), (2, 1, 42))

    def test_model_prefix_tool_name(self):
        self.assertEqual(self.summary["model"], "kimi-code · kimi-k3")
        self.assertEqual(self.summary["raw_model"], "kimi-k3")

    def test_elapsed_is_last_turn_only(self):
        # 最后一轮 101000 → 104500 = 3.5s，而不是全程 104.5s
        self.assertEqual(self.summary["elapsed"], "3.5s")

    def test_context_with_known_window(self):
        self.assertEqual(kimi.fmt_context(self.summary["ctx"], kimi.context_window("kimi-k3", self.home)),
                         "153.6k/1.0m (15%)")

    def test_find_wire(self):
        self.assertIsNone(kimi.find_wire("session_nope", self.home))
        self.assertIsNone(kimi.find_wire("", self.home))


class FmtContextTest(unittest.TestCase):
    def test_without_limit_falls_back_to_absolute(self):
        self.assertEqual(kimi.fmt_context(153_600, 0), "153.6k")

    def test_empty(self):
        self.assertEqual(kimi.fmt_context(0, 1_048_576), "")

    def test_percentage_rounding(self):
        self.assertEqual(kimi.fmt_context(524_288, 1_048_576), "524.3k/1.0m (50%)")


class ContextWindowTest(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(kimi.context_window("k3", Path("/nonexistent")), 1_048_576)
        self.assertEqual(kimi.context_window("kimi-for-coding", Path("/nonexistent")), 262_144)

    def test_unknown_model_no_config(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kimi.context_window("mystery-1", Path(d)), 0)

    def test_unknown_model_from_config_toml(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "config.toml").write_text(
                '[models."acme/mystery-1"]\nmodel = "mystery-1"\nmax_context_size = 131072\n',
                encoding="utf-8")
            self.assertEqual(kimi.context_window("mystery-1", Path(d)), 131_072)


class GateTest(unittest.TestCase):
    def test_default_on_explicit_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEIGE_HOOK_NOTIFY", None)
            self.assertTrue(kimi.hook_enabled())
        with mock.patch.dict(os.environ, {"FEIGE_HOOK_NOTIFY": "0"}):
            self.assertFalse(kimi.hook_enabled())

    def test_stop_dispatch_detects_kimi(self):
        spec = importlib.util.spec_from_file_location("feige_stop", REPO / "hooks" / "stop.py")
        stop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stop)
        self.assertEqual(stop.detect_host({"client_type": "kimi_code_cli"}, env={}), "kimi-code")
        self.assertEqual(stop.detect_host({"turn_id": "t1"}, env={}), "codex")
        self.assertEqual(stop.detect_host({"transcript_path": "/x/.claude/t.jsonl"}, env={}), "claude-code")


if __name__ == "__main__":
    unittest.main()
