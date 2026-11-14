"""Offline wire/security contracts; no Hermes installation or network required."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
common = types.ModuleType("agent.web_search_provider")
class Base:
    def supports_search(self): return True
    def supports_extract(self): return self.EXTRACT
common.WebSearchProvider = Base
common.get_provider_env = lambda name: "test-profile-key"
ssl = types.ModuleType("agent.ssl_verify")
ssl.requests_verify = lambda: True
config = types.ModuleType("hermes_cli.config")
settings = {"base_url": "https://gateway.example/v1", "search_provider": "keenable",
            "fetch_provider": "keenable-search"}
config.load_config_readonly = lambda: {"web": {"omniroute": settings}}
for name, module in [("agent.web_search_provider", common), ("agent.ssl_verify", ssl),
                     ("hermes_cli.config", config)]:
    sys.modules[name] = module
spec = importlib.util.spec_from_file_location("web_candidate", ROOT / "plugins/web/omniroute/provider.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class WebContracts(unittest.TestCase):
    def setUp(self):
        self.provider = module.OmniRouteWebProvider()
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"provider": "keenable-search", "results": []}
        self.transport = patch("requests.post", return_value=self.response)
        self.post = self.transport.start()
        self.addCleanup(self.transport.stop)

    def test_search_wire_contract_and_result_normalization(self):
        self.response.json.return_value = {"provider": "keenable-search", "results": [
            {"url": "https://docs.example/page", "title": "Docs", "snippet": "x" * 900},
            {"url": "javascript:alert(1)"}, None]}
        result = self.provider.search(" public query ", 3)
        args, kw = self.post.call_args
        self.assertEqual(args[0], "https://gateway.example/v1/search")
        self.assertEqual(kw["json"], {"query": "public query", "max_results": 3, "provider": "keenable"})
        self.assertEqual(len(result["data"]["web"]), 1)
        self.assertEqual(len(result["data"]["web"][0]["description"]), 500)
        self.assertFalse(kw["allow_redirects"])

    def test_search_count_is_clamped(self):
        self.provider.search("query", 500)
        self.assertEqual(self.post.call_args.kwargs["json"]["max_results"], 50)

    def test_empty_query_does_not_dispatch(self):
        self.assertFalse(self.provider.search(" ")["success"])
        self.post.assert_not_called()

    def test_profile_secret_resolved_each_call(self):
        with patch.object(module, "provider_env", side_effect=["profile-a", "profile-b"]):
            self.provider.search("one")
            self.provider.search("two")
        auth = [c.kwargs["headers"]["Authorization"] for c in self.post.call_args_list]
        self.assertEqual(auth, ["Bearer profile-a", "Bearer profile-b"])

    def test_scoped_miss_never_uses_process_key(self):
        with patch.dict(os.environ, {"OMNIROUTE_API_KEY": "launch-key"}), patch.object(module, "provider_env", return_value=""):
            result = self.provider.search("query")
        self.assertFalse(result["success"])
        self.post.assert_not_called()

    def test_unbound_scope_error_fails_closed(self):
        with patch.object(module, "provider_env", side_effect=RuntimeError("scope unavailable")):
            self.assertFalse(self.provider.is_available())
            self.assertFalse(self.provider.search("query")["success"])
        self.post.assert_not_called()

    def test_config_provider_is_not_import_time_global(self):
        with patch.object(module, "_settings", return_value={**settings, "search_provider": "other-search"}):
            self.provider.search("query")
        self.assertEqual(self.post.call_args.kwargs["json"]["provider"], "other-search")

    def test_unsafe_gateway_urls_do_not_dispatch(self):
        for base in ["http://gateway.example/v1", "https://u:p@gateway.example/v1",
                     "https://gateway.example/v1?token=secret", "https://gateway.example"]:
            with self.subTest(base=base), patch.object(module, "_settings", return_value={"base_url": base}):
                self.assertFalse(self.provider.search("query")["success"])
        self.post.assert_not_called()

    def test_redirect_and_403_are_not_retried_or_dumped(self):
        for status in [302, 403, 502]:
            self.response.status_code = status
            result = self.provider.search("query")
            self.assertEqual(result["error"], "OmniRoute returned HTTP " + str(status))
        self.assertEqual(self.post.call_count, 3)

    def test_timeout_does_not_retry_or_leak_exception(self):
        self.post.side_effect = requests.Timeout("secret upstream URL")
        result = self.provider.search("query")
        self.assertIn("timed out", result["error"])
        self.assertNotIn("secret", result["error"])
        self.post.assert_called_once()

    def test_invalid_json_is_clear_failure(self):
        self.response.json.side_effect = ValueError("bad")
        self.assertFalse(self.provider.search("query")["success"])
        self.response.close.assert_called_once()

    def test_extract_wire_contract_and_truncation_metadata(self):
        self.response.json.return_value = {"provider": "keenable-search", "content": "Markdown body",
                                           "metadata": {"title": "Docs", "truncated": True}}
        result = self.provider.extract(["https://docs.example/page"])[0]
        self.assertEqual(result["content"], result["raw_content"])
        self.assertTrue(result["metadata"]["truncated"])
        self.assertEqual(result["title"], "Docs")
        self.assertEqual(self.post.call_args.args[0], "https://gateway.example/v1/web/fetch")
        self.assertEqual(self.post.call_args.kwargs["json"], {"url": "https://docs.example/page",
                         "provider": "keenable-search", "format": "markdown", "include_metadata": True})

    def test_extract_batch_preserves_partial_failure_and_order(self):
        one = Mock(status_code=403)
        two = Mock(status_code=200)
        two.json.return_value = {"content": "Second page", "metadata": {"title": "Two"}}
        self.post.side_effect = [one, two]
        urls = ["https://docs.example/one", "https://docs.example/two"]
        result = self.provider.extract(urls)
        self.assertEqual([x["url"] for x in result], urls)
        self.assertIn("403", result[0]["error"])
        self.assertEqual(result[1]["content"], "Second page")

    def test_empty_extraction_is_failure(self):
        self.response.json.return_value = {"content": " "}
        self.assertIn("error", self.provider.extract(["https://docs.example"])[0])

    def test_non_http_input_does_not_dispatch(self):
        self.assertIn("error", self.provider.extract(["file:///etc/passwd"])[0])
        self.post.assert_not_called()

    def test_batch_limit_fails_without_silent_drop(self):
        results = self.provider.extract(["https://docs.example"] * 21)
        self.assertEqual(len(results), 21)
        self.assertTrue(all("error" in r for r in results))
        self.post.assert_not_called()

if __name__ == "__main__":
    unittest.main()
