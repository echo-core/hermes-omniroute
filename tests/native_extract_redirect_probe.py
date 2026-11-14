"""Native redirect/cache regression using disposable profiles and mocked HTTP.

Run with Hermes's managed Python/bootstrap. No provider request is sent.
The original adapter fails the private-target assertion; the repaired adapter
passes it and verifies final-URL cache checks. Public targets use native DNS safety.
"""

import argparse
import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "backend/web.py"
    )
    args = parser.parse_args()
    source_code = args.source.read_text()
    with tempfile.TemporaryDirectory(prefix="omniroute-redirect-") as directory:
        root = Path(directory)
        managed = root / "managed"
        managed.mkdir()
        os.environ["HERMES_MANAGED_DIR"] = str(managed)
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_cli.plugins import discover_plugins
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        @contextmanager
        def scope(name, source):
            home = root / name
            plugin = home / "plugins/redirect-fixture"
            plugin.mkdir(parents=True)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [redirect-fixture]\n  isolation: in_process\nweb:\n  extract_backend: omniroute\n  keyless_rescue: false\n  omniroute:\n    base_url: https://gateway.example/v1\n"
            )
            (plugin / "plugin.yaml").write_text(
                "name: redirect-fixture\nkind: backend\nversion: 0.0.1\n"
            )
            (plugin / "provider.py").write_text(source)
            (plugin / "__init__.py").write_text(
                "def register(ctx):\n    from .provider import OmniRouteWebProvider\n    ctx.register_web_search_provider(OmniRouteWebProvider())\n"
            )
            token = set_hermes_home_override(home)
            secret = set_secret_scope(
                {"OMNIROUTE_API_KEY": "fixture-" + name}, profile_home=str(home)
            )
            try:
                discover_plugins(force=True)
                yield
            finally:
                reset_secret_scope(secret)
                reset_hermes_home_override(token)

        from tools.web_tools import web_extract_tool

        outcomes = []
        for name, source in [("new", source_code)]:
            with scope(name, source):
                response = Mock(status_code=200)
                response.json.return_value = {
                    "url": "http://127.0.0.1/private-fixture",
                    "content": "Redirected private fixture",
                    "provider": "keenable-search",
                    "metadata": {"title": "Fixture"},
                }
                with patch("requests.post", return_value=response):
                    data = json.loads(
                        asyncio.run(web_extract_tool(["https://example.com/?redirect=" + name]))
                    )
                served = "Redirected private fixture" in json.dumps(data)
                assert not served, data
                assert "private" in json.dumps(data).lower(), data
                outcomes.append({"source": name, "privateFixtureServed": served})
        with scope("cache", source_code):
            from tools.web_result_cache import extract_cache_get

            response = Mock(status_code=200)
            requested = "https://example.com/?redirect-cache-fixture=1"
            final = "https://example.org/"
            response.json.return_value = {
                "url": final,
                "content": "Public redirected fixture",
                "metadata": {"title": "Public fixture"},
            }
            with patch("requests.post", return_value=response) as post:
                data = json.loads(asyncio.run(web_extract_tool([requested])))
                assert "Public redirected fixture" in json.dumps(data), data
                cached = extract_cache_get(requested, format=None, provider="omniroute")
                assert cached and cached["final_url"] == final, cached

            async def safe_url(url):
                return url != final

            with (
                patch("requests.post", return_value=response) as post,
                patch("tools.url_safety.async_is_safe_url", side_effect=safe_url),
            ):
                data = json.loads(asyncio.run(web_extract_tool([requested])))
                assert "Public redirected fixture" not in json.dumps(data), data
                assert post.call_count == 1, post.call_count
            outcomes.append(
                {"publicRedirectCachedByRequestedURL": True, "cachedFinalURLRegated": True}
            )
        print("NATIVE_REDIRECT_REGRESSION " + json.dumps(outcomes))


if __name__ == "__main__":
    main()
