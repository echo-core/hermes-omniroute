"""Native two-package lifecycle and tool contracts using disposable profiles."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

MODEL_PLUGIN = "hermes-omniroute-provider"
BACKEND_PLUGIN = "hermes-omniroute-backend"
SOURCE = "https://github.com/echo-core/hermes-omniroute.git"


def require_ok(result, action):
    if not result.get("ok"):
        raise AssertionError(action + ": " + str(result.get("error", result)))


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-pair-") as directory:
        root = Path(directory)
        managed = root / "managed"
        managed.mkdir()
        os.environ["HERMES_MANAGED_DIR"] = str(managed)
        isolation = os.environ.get("OMNIROUTE_PROBE_ISOLATION", "in_process")
        homes = [root / name for name in ("a", "b")]
        for home in homes:
            home.mkdir()
            (home / "config.yaml").write_text(
                """plugins:
  enabled: []
  isolation: MODE
model:
  provider: omniroute
  default: auto
  base_url: https://gateway.example/v1
web:
  search_backend: omniroute
  extract_backend: omniroute
  keyless_rescue: false
  omniroute:
    base_url: https://gateway.example/v1
    search_provider: keenable
    fetch_provider: keenable-search
image_gen:
  provider: omniroute-images
  omniroute-images:
    base_url: https://gateway.example/v1
""".replace("MODE", isolation)
            )
        os.environ["HERMES_HOME"] = str(homes[0])
        from agent.image_gen_registry import get_provider as get_image
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from agent.web_search_registry import get_provider as get_web
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled
        from hermes_cli.plugins_cmd_install import dashboard_install_plugin
        from hermes_cli.plugins_cmd_remove import dashboard_remove_user_plugin
        from hermes_cli.plugins_cmd_update import dashboard_update_user_plugin
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from providers import get_provider_profile
        from tools.registry import registry

        @contextmanager
        def bind(home):
            token = set_hermes_home_override(home)
            secret = set_secret_scope(
                {"OMNIROUTE_API_KEY": "fixture-" + home.name}, profile_home=str(home)
            )
            try:
                yield
            finally:
                reset_secret_scope(secret)
                reset_hermes_home_override(token)

        def model_owned_fresh(home):
            import hermes_constants

            native_root = str(Path(hermes_constants.__file__).parent)
            code = (
                "import sys;sys.path.insert(0," + repr(native_root) + ");"
                "sys._hermes_pin_default_home=True;import hermes_bootstrap;"
                "from hermes_constants import set_hermes_home_override;"
                "set_hermes_home_override(" + repr(str(home)) + ");"
                "from providers import provider_source;"
                "print('MODEL_OWNED='+str(provider_source('omniroute')=='user'))"
            )
            output = subprocess.check_output([sys.executable, "-I", "-c", code], text=True)
            return "MODEL_OWNED=True" in output

        model_cache_retained = False
        checks = []
        for home in homes:
            with bind(home):
                for folder in ("model-provider", "backend"):
                    require_ok(
                        dashboard_install_plugin(SOURCE + "#" + folder, force=False, enable=True),
                        "install " + folder,
                    )
                discover_plugins(force=True)
                rows = [
                    r
                    for r in get_plugin_manager().list_plugins()
                    if r["name"] in (MODEL_PLUGIN, BACKEND_PLUGIN)
                ]
                assert len(rows) == 2 and all(r["enabled"] and not r["error"] for r in rows), rows
                assert get_provider_profile("omniroute") is not None
                assert get_web("omniroute") is not None
                assert get_image("omniroute-images") is not None
                assert registry.get_entry("image_generate_complex") is not None
                checks.append("installed-" + home.name)
            if home == homes[0]:
                with bind(homes[1]):
                    assert get_provider_profile("omniroute") is None
                    assert get_web("omniroute") is None
                    assert get_image("omniroute-images") is None
                    assert registry.get_entry("image_generate_complex") is None
                checks.append("uninstalled-profile-isolated")

        if isolation == "in_process":
            import requests  # noqa: F401 - load shared transport before patching requests.post
            from tools.web_tools import web_extract_tool, web_search_tool

            for home in homes:
                with bind(home):
                    response = Mock(status_code=200)
                    response.json.return_value = {
                        "provider": "keenable-search",
                        "results": [
                            {
                                "title": "Fixture result",
                                "url": "https://example.com/",
                                "snippet": "Scoped fixture result",
                            }
                        ],
                    }
                    with patch("requests.post", return_value=response) as post:
                        result = web_search_tool("Synthetic lifecycle search " + home.name, limit=1)
                        assert "Fixture result" in result, result
                        assert post.call_args.args[0] == "https://gateway.example/v1/search"
                        assert (
                            post.call_args.kwargs["headers"]["Authorization"]
                            == "Bearer fixture-" + home.name
                        )
                    response = Mock(status_code=200)
                    response.json.return_value = {
                        "provider": "keenable-search",
                        "content": "# Native fixture content",
                        "metadata": {"title": "Fixture page"},
                    }
                    with patch("requests.post", return_value=response) as post:
                        result = asyncio.run(
                            web_extract_tool(["https://example.com/?fixture=" + home.name])
                        )
                        assert "Native fixture content" in result, result
                        assert post.call_args.args[0] == "https://gateway.example/v1/web/fetch"
                        assert (
                            post.call_args.kwargs["headers"]["Authorization"]
                            == "Bearer fixture-" + home.name
                        )
                    denied = Mock(status_code=403)
                    with (
                        patch("requests.post", return_value=denied) as post,
                        patch(
                            "tools.web_tools_rescue._rescue_search",
                            side_effect=AssertionError("Direct search fallback"),
                        ),
                        patch(
                            "tools.web_tools_rescue._rescue_extract",
                            side_effect=AssertionError("Direct extract fallback"),
                        ),
                    ):
                        result = web_search_tool("Synthetic denied search " + home.name, limit=1)
                        assert "403" in result, result
                        result = asyncio.run(
                            web_extract_tool(["https://example.com/?denied=" + home.name])
                        )
                        assert "403" in result, result
                        assert post.call_count == 2
                    with patch(
                        "requests.post", side_effect=AssertionError("Private URL dispatched")
                    ) as post:
                        result = asyncio.run(web_extract_tool(["http://127.0.0.1/private-fixture"]))
                        assert "private" in result.lower() or "internal" in result.lower(), result
                        post.assert_not_called()
                checks.append("native-web-and-errors-" + home.name)

        with bind(homes[0]):
            for name in (BACKEND_PLUGIN, MODEL_PLUGIN):
                require_ok(
                    dashboard_set_agent_plugin_enabled(name, enabled=False), "disable " + name
                )
            discover_plugins(force=True)
            model_cache_retained = get_provider_profile("omniroute") is not None
            assert not model_owned_fresh(homes[0])
            assert get_web("omniroute") is None
            assert get_image("omniroute-images") is None
            assert registry.get_entry("image_generate_complex") is None
        with bind(homes[1]):
            assert get_provider_profile("omniroute") is not None
            assert get_web("omniroute") is not None
            assert registry.get_entry("image_generate_complex") is not None
        checks.append("disable-isolated")
        with bind(homes[0]):
            for name in (MODEL_PLUGIN, BACKEND_PLUGIN):
                require_ok(dashboard_set_agent_plugin_enabled(name, enabled=True), "enable " + name)
            discover_plugins(force=True)
            assert get_provider_profile("omniroute") and get_web("omniroute")
            for name in (MODEL_PLUGIN, BACKEND_PLUGIN):
                require_ok(dashboard_update_user_plugin(name), "update " + name)
            checks.append("reenable-and-update")
            for name in (BACKEND_PLUGIN, MODEL_PLUGIN):
                require_ok(dashboard_remove_user_plugin(name), "remove " + name)
            discover_plugins(force=True)
            model_cache_retained = get_provider_profile("omniroute") is not None
            assert not model_owned_fresh(homes[0])
            assert get_web("omniroute") is None
            assert get_image("omniroute-images") is None
            assert registry.get_entry("image_generate_complex") is None
        with bind(homes[1]):
            assert get_provider_profile("omniroute") and get_web("omniroute")
            assert registry.get_entry("image_generate_complex") is not None
        checks.append("remove-isolated")
        print(
            "NATIVE_PAIR_ACCEPTANCE "
            + json.dumps(
                {
                    "isolation": isolation,
                    "checks": checks,
                    "model_disable_remove_verified_after_fresh_start": True,
                    "in_process_model_cache_retained": model_cache_retained,
                }
            )
        )


if __name__ == "__main__":
    main()
