"""版本号一致性：主插件 feige.__version__ 是唯一来源，面板与两份插件清单必须同步。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import feige  # noqa: E402
import webui  # noqa: E402


class VersionSyncTest(unittest.TestCase):
    def test_panel_version_matches_core(self):
        self.assertEqual(webui.VERSION, feige.__version__)

    def test_plugin_manifests_match_core(self):
        for rel in (".claude-plugin/plugin.json", ".zcode-plugin/plugin.json"):
            with self.subTest(manifest=rel):
                doc = json.loads((REPO / rel).read_text(encoding="utf-8"))
                self.assertEqual(doc["version"], feige.__version__)

    def test_marketplace_listings_match_core(self):
        for rel in (".claude-plugin/marketplace.json", ".zcode-plugin/marketplace.json"):
            with self.subTest(marketplace=rel):
                doc = json.loads((REPO / rel).read_text(encoding="utf-8"))
                for entry in doc["plugins"]:
                    self.assertEqual(entry["version"], feige.__version__)


if __name__ == "__main__":
    unittest.main()
