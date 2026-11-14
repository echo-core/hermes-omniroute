"""Check native warm/disk context caches with separate keys at one gateway.

Requires Hermes's managed Python/bootstrap. All model catalog HTTP is mocked.
Only disposable profile homes and their native metadata caches are written.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from unittest.mock import MagicMock
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path,
        default=Path(__file__).resolve().parents[1] / "model-provider/__init__.py",
    )
    source_code = parser.parse_args().source.read_text()

    with tempfile.TemporaryDirectory(prefix="omniroute-context-") as directory:
        root = Path(directory)
        managed = root / "managed"
        managed.mkdir()
        os.environ["HERMES_MANAGED_DIR"] = str(managed)

        import hermes_constants
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from providers import get_provider_profile

        @contextmanager
        def bind(home, key):
            home_token = set_hermes_home_override(home)
            secret_token = set_secret_scope(
                {"OMNIROUTE_API_KEY": key}, profile_home=str(home),
            )
            try:
                yield
            finally:
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)

        homes = []
        for name in ("a", "b"):
            home = root / name
            plugin = home / "plugins/model-providers/context-fixture"
            plugin.mkdir(parents=True)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [context-fixture]\n"
                "model:\n  provider: omniroute\n  default: auto\n"
                "  base_url: https://gateway.example/v1\n"
            )
            (plugin / "plugin.yaml").write_text(
                "name: context-fixture\nkind: model-provider\nversion: 0.0.1\n"
            )
            (plugin / "__init__.py").write_text(source_code)
            homes.append(home)

        catalog_calls = []
        contexts = {"fixture-a": 16384, "fixture-b": 131072}

        def catalog_response(url, **kwargs):
            assert url == "https://gateway.example/v1/models"
            key = kwargs["headers"]["Authorization"].split(" ", 1)[1]
            catalog_calls.append(key)
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "data": [{"id": "auto", "context_length": contexts[key]}],
            }
            response.__enter__.return_value = response
            return response

        with patch("agent.model_metadata.model_metadata_http.stream", side_effect=catalog_response):
            for index in (0, 1, 0, 1):
                home = homes[index]
                key = "fixture-" + home.name
                with bind(home, key):
                    value = get_provider_profile("omniroute").get_model_context_length("auto")
                    assert value == contexts[key], (home.name, value)
        assert catalog_calls == ["fixture-a", "fixture-b"], catalog_calls

        native_root = str(Path(hermes_constants.__file__).parent)
        for home in homes:
            key = "fixture-" + home.name
            child_code = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {native_root!r})
                sys._hermes_pin_default_home = True
                import hermes_bootstrap
                from hermes_constants import set_hermes_home_override
                from agent.secret_scope import set_secret_scope
                set_hermes_home_override({str(home)!r})
                set_secret_scope({{"OMNIROUTE_API_KEY": {key!r}}}, profile_home={str(home)!r})
                from providers import get_provider_profile
                from unittest.mock import patch
                with patch("agent.model_metadata.model_metadata_http.stream",
                           side_effect=AssertionError("Unexpected network probe")):
                    value = get_provider_profile("omniroute").get_model_context_length("auto")
                    assert value == {contexts[key]}, value
                print("COLD_CONTEXT_OK")
            """)
            output = subprocess.check_output([sys.executable, "-I", "-c", child_code], text=True)
            assert "COLD_CONTEXT_OK" in output

        print("NATIVE_CONTEXT_CACHE " + json.dumps({
            "sameGateway": True, "distinctKeys": True,
            "values": list(contexts.values()), "warmScopeSwitches": True,
            "freshProcessDiskCaches": True, "catalogProbes": len(catalog_calls),
        }))


if __name__ == "__main__":
    main()
