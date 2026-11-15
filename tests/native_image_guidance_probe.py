"""Exercise prompt lifecycle in disposable Hermes homes, without provider requests.

Run through Hermes's managed runtime/bootstrap. Set OMNIROUTE_PROBE_ISOLATION to
in_process or host; neither installs into the caller's real profile.
"""

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

NAME = "hermes-omniroute-backend"
SECTION = "omniroute.image-routing"


def main():
    isolation = os.environ.get("OMNIROUTE_PROBE_ISOLATION", "in_process")
    assert isolation in ("in_process", "host")
    with tempfile.TemporaryDirectory(prefix="hermes-image-guidance-") as td:
        root = Path(td)
        source = root / "source"
        shutil.copytree(Path(__file__).resolve().parents[1] / "backend", source / "backend")
        for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "Local fixture"]):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        managed = root / "managed"
        managed.mkdir()
        os.environ["HERMES_MANAGED_DIR"] = str(managed)
        homes = [root / name for name in ("a", "b", "c")]
        for home in homes:
            home.mkdir()
            (home / ".env").write_text("OMNIROUTE_API_KEY=fixture-" + home.name + "\n")
            (home / ".env").chmod(0o600)
            selected = "another" if home.name == "b" else "omniroute-images"
            (home / "config.yaml").write_text(
                f"plugins:\n  isolation: {isolation}\n  enabled: []\n"
                f"image_gen:\n  provider: {selected}\n  omniroute-images:\n"
                "    base_url: https://gateway.example/v1\n"
            )
        os.environ["HERMES_HOME"] = str(homes[0])
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_cli.plugins import discover_plugins, render_system_prompt_sections
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled
        from hermes_cli.plugins_cmd_install import dashboard_install_plugin
        from hermes_cli.plugins_cmd_remove import dashboard_remove_user_plugin
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        @contextmanager
        def bind(home, available=True):
            token = set_hermes_home_override(home)
            values = {"OMNIROUTE_API_KEY": "fixture-" + home.name}  # pragma: allowlist secret
            secret = set_secret_scope(values if available else {}, profile_home=str(home))
            try:
                yield
            finally:
                reset_secret_scope(secret)
                reset_hermes_home_override(token)

        def section():
            return [
                s
                for s in render_system_prompt_sections({"session_id": "fixture"})
                if s.id == SECTION
            ]

        def require(result):
            assert result.get("ok"), result

        for home in homes[:2]:
            with bind(home):
                require(
                    dashboard_install_plugin(source.as_uri() + "#backend", force=False, enable=True)
                )
                discover_plugins(force=True)
                assert bool(section()) == (home == homes[0]), "selected profile " + home.name
        with bind(homes[2]):
            assert not section()  # installed elsewhere is not installed here
        # Child hosts resolve their own on-disk profile secret scope. Select a
        # nonexistent credential name without re-registering the prompt callback.
        config_path = homes[0] / "config.yaml"
        config = config_path.read_text()
        config_path.write_text(config + "    key_env: OMNIROUTE_PROBE_MISSING_KEY\n")
        with bind(homes[0], available=False):
            assert not section()  # availability is read again at render time
        config_path.write_text(config)
        with bind(homes[0]):
            assert len(section()) == 1
            assert "tool_describe" in section()[0].content
            require(dashboard_set_agent_plugin_enabled(NAME, enabled=False))
            discover_plugins(force=True)
            assert not section()
            require(dashboard_set_agent_plugin_enabled(NAME, enabled=True))
            discover_plugins(force=True)
            assert len(section()) == 1
            require(dashboard_remove_user_plugin(NAME))
            discover_plugins(force=True)
            assert not section()
        with bind(homes[1]):
            assert not section()
        print(
            "NATIVE_IMAGE_GUIDANCE "
            + json.dumps(
                {
                    "isolation": isolation,
                    "selectedProfile": True,
                    "otherProviderSuppressed": True,
                    "uninstalledProfileSuppressed": True,
                    "missingCredentialSuppressed": True,
                    "disableReenableRemove": True,
                    "providerCalls": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
