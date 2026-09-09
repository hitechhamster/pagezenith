"""Offline regressions for provider egress routing; no real credentials or requests."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
from tools.seo_gap.config import Settings


class ProxyRoutingTests(unittest.TestCase):
    def settings(self, **overrides):
        with patch.dict("os.environ", {}, clear=True):
            return Settings(_env_file=None, outbound_proxy="http://127.0.0.1:8888", **overrides)

    def test_openrouter_text_stream_and_image_base(self):
        s = self.settings()
        self.assertEqual(s.proxy_for(s.openrouter_base_url), s.outbound_proxy)
        self.assertEqual(s.proxy_for("openrouter"), s.outbound_proxy)

    def test_existing_routes_unchanged(self):
        s = self.settings()
        self.assertEqual(s.proxy_for(s.gemini_base_url), s.outbound_proxy)
        self.assertEqual(s.proxy_for("gemini"), s.outbound_proxy)
        for target in ("deepseek", "https://api.deepseek.com", "serper", "reddit", "dodo"):
            self.assertIsNone(s.proxy_for(target))

    def test_without_proxy(self):
        s = self.settings().model_copy(update={"outbound_proxy": ""})
        self.assertIsNone(s.proxy_for(s.openrouter_base_url))

    def test_explicit_targets_respected(self):
        for targets in ("", "gemini"):
            self.assertIsNone(self.settings(proxy_targets=targets).proxy_for("openrouter"))

    def test_byok_keeps_proxy_and_user_key(self):
        import byok
        s = self.settings()
        with patch.object(byok, "get_settings", return_value=s):
            result = byok.settings_for(byok.ByokConfig(
                llm_key="test-user-key", serper_key="test-search-key", provider="openrouter"))
        self.assertEqual(result.openrouter_api_key, "test-user-key")
        self.assertEqual(result.proxy_for(result.openrouter_base_url), s.outbound_proxy)
        self.assertEqual(result.force_llm_provider, "openrouter")
        self.assertEqual(s.openrouter_api_key, "")


if __name__ == "__main__":
    unittest.main()
