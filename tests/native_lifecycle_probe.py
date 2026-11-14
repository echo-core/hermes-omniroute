"""Reproduce the model/backend lifecycle gap using an installed Hermes runtime.

Run with Hermes's managed Python and bootstrap, not the offline test stubs.
Only disposable profile homes and a local Git fixture are written. No provider
requests are made. A successful reproduction exits zero and reports supported=false.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile

NAME = "omniroute-lifecycle-fixture"
MODEL = "omniroute-lifecycle-model"
WEB = "omniroute-lifecycle-web"
SOURCE = r'''from providers import register_provider
from providers.base import ProviderProfile

register_provider(ProviderProfile(
    name="omniroute-lifecycle-model", env_vars=("FIXTURE_KEY",),
    base_url="https://gateway.example/v1", auth_type="api_key"))

# Model discovery never invokes this backend entry point on Hermes 0.23.1.
def register(ctx):
    from agent.web_search_provider import WebSearchProvider

    class FixtureWeb(WebSearchProvider):
        @property
        def name(self):
            return "omniroute-lifecycle-web"

        def is_available(self):
            return True

        def search(self, query, limit=5):
            return {"success": True, "data": {"web": []}}

    ctx.register_web_search_provider(FixtureWeb())
'''


def git(directory, *args):
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-C", str(directory), *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-lifecycle-") as directory:
        root = Path(directory)
        home_a, home_b, managed = (root / name for name in ("a", "b", "managed"))
        for home in (home_a, home_b, managed):
            home.mkdir()
        isolation = os.environ.get("OMNIROUTE_PROBE_ISOLATION", "in_process")
        if isolation not in ("in_process", "host"):
            raise ValueError("Choose in_process or host")
        os.environ["HERMES_HOME"] = str(home_a)
        os.environ["HERMES_MANAGED_DIR"] = str(managed)
        for home in (home_a, home_b):
            (home / "config.yaml").write_text(
                f"plugins:\n  isolation: {isolation}\n  enabled: [{NAME}]\n"
            )
        source = root / NAME
        source.mkdir()
        (source / "plugin.yaml").write_text(
            f"name: {NAME}\nversion: 0.0.1\nkind: model-provider\n"
        )
        (source / "__init__.py").write_text(SOURCE)
        git(source, "init", "-q")
        git(source, "add", ".")
        git(source, "commit", "-qm", "Local lifecycle fixture")

        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_cli.plugins_cmd_install import dashboard_install_plugin
        from hermes_cli.plugins import get_plugin_manager
        from agent.web_search_registry import get_provider
        from providers import get_provider_profile

        @contextmanager
        def bind(home, key):
            token = set_hermes_home_override(home)
            secret = set_secret_scope({"FIXTURE_KEY": key}, profile_home=str(home))
            try:
                yield
            finally:
                reset_secret_scope(secret)
                reset_hermes_home_override(token)

        result = {"isolation": isolation}
        with bind(home_a, "fixture-a"):
            installed = dashboard_install_plugin(source.as_uri(), force=False, enable=False)
            if not installed.get("ok"):
                raise RuntimeError("Native fixture installation failed: " + str(installed.get("error")))
            result["native_install"] = True
            # Resolve before manager startup, matching early auth/provider discovery.
            result["model_a"] = get_provider_profile(MODEL) is not None
            manager = get_plugin_manager()
            manager.discover_and_load()
            rows = [row for row in manager.list_plugins() if row["name"] == NAME]
            result["inventory_rows"] = len(rows)
            result["web_a"] = get_provider(WEB) is not None
        with bind(home_b, "fixture-b"):
            result["model_b"] = get_provider_profile(MODEL) is not None
            result["web_b"] = get_provider(WEB) is not None
        result["supported"] = (
            result["model_a"] and result["web_a"] and result["inventory_rows"] == 1
            and not result["model_b"] and not result["web_b"]
        )
        # This is a regression reproduction, not acceptance of the failed design.
        assert result["model_a"] and not result["web_a"], result
        assert result["inventory_rows"] == 1, result
        assert not result["model_b"] and not result["web_b"], result
        assert not result["supported"], result
        print("LIFECYCLE_REPRODUCTION " + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
