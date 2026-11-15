"""Offline context-routing regressions for the generic model component."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class ModelContracts(unittest.TestCase):
    def setUp(self):
        self.config = {"model": {"provider": "omniroute", "base_url": "https://gateway.example/v1"}}
        self.secrets = {
            "OMNIROUTE_API_KEY": "profile-a",  # pragma: allowlist secret
            "OMNIROUTE_BASE_URL": "https://env.example/v1",
        }
        self.lookup = Mock(return_value=65536)
        self.registration = Mock()
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: self.config
        secret = types.ModuleType("agent.web_search_provider")
        secret.get_provider_env = lambda name: self.secrets.get(name, "")
        metadata = types.ModuleType("agent.model_metadata")
        metadata._resolve_custom_endpoint_context_length = self.lookup
        providers = types.ModuleType("providers")
        providers.register_provider = self.registration
        base = types.ModuleType("providers.base")

        class Profile:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        base.ProviderProfile = Profile
        self.modules = patch.dict(
            sys.modules,
            {
                "hermes_cli.config": config,
                "agent.web_search_provider": secret,
                "agent.model_metadata": metadata,
                "providers": providers,
                "providers.base": base,
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location(
            "model_fixture", ROOT / "model-provider/__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.profile = self.registration.call_args.args[0]

    def test_endpoint_environment_variable_is_never_an_api_key_alias(self):
        self.assertEqual(self.profile.env_vars, ("OMNIROUTE_API_KEY",))
        self.assertEqual(self.profile.base_url_env_var, "OMNIROUTE_BASE_URL")
        self.assertTrue(self.profile.aggregator)
        self.assertEqual(self.profile.default_aux_model, "auto/fast")

    def test_auxiliary_runtime_gets_configured_endpoint_without_catalog_lookup(self):
        endpoint = self.profile.runtime_base_url(
            api_key=self.secrets["OMNIROUTE_API_KEY"], default="", env_url=""
        )
        self.assertEqual(endpoint, "https://gateway.example/v1")
        self.lookup.assert_not_called()

    def test_auxiliary_runtime_follows_current_profile_without_reusing_prior_url(self):
        self.assertEqual(
            self.profile.runtime_base_url(
                api_key=self.secrets["OMNIROUTE_API_KEY"], default="", env_url=""
            ),
            "https://gateway.example/v1",
        )
        self.config = {
            "providers": {"omniroute": {"api": "https://second.example/v1/"}},
            "model": {"provider": "another", "base_url": "https://another.example/v1"},
        }
        self.assertEqual(
            self.profile.runtime_base_url(
                api_key=self.secrets["OMNIROUTE_API_KEY"], default="", env_url=""
            ),
            "https://second.example/v1",
        )
        self.lookup.assert_not_called()

    def test_configured_endpoint_and_scoped_key_reach_native_context_resolver(self):
        self.assertEqual(self.profile.get_model_context_length("auto"), 65536)
        self.lookup.assert_called_once_with(
            "auto", "https://gateway.example/v1", "profile-a", "omniroute"
        )

    def test_named_provider_endpoint_is_used_for_auxiliary_routes(self):
        self.config = {
            "providers": {"omniroute": {"api": "https://named.example/v1/"}},
            "model": {"provider": "another", "base_url": "https://another.example/v1"},
        }
        self.profile.get_model_context_length("auto/fast")
        self.assertEqual(self.lookup.call_args.args[1], "https://named.example/v1")

    def test_environment_endpoint_works_when_main_model_uses_another_provider(self):
        self.config = {"model": {"provider": "another", "base_url": "https://another.example/v1"}}
        self.profile.get_model_context_length("auto/fast")
        self.assertEqual(self.lookup.call_args.args[1], "https://env.example/v1")

    def test_auxiliary_endpoint_never_uses_another_providers_url(self):
        self.config = {"model": {"provider": "another", "base_url": "https://another.example/v1"}}
        self.secrets.pop("OMNIROUTE_BASE_URL")
        with self.assertRaisesRegex(ValueError, "trusted OmniRoute HTTPS endpoint"):
            self.profile.runtime_base_url(
                api_key=self.secrets["OMNIROUTE_API_KEY"], default="", env_url=""
            )
        self.lookup.assert_not_called()

    def test_same_gateway_uses_each_current_profile_key(self):
        self.lookup.side_effect = [65536, 32768]
        self.assertEqual(self.profile.get_model_context_length("auto"), 65536)
        self.secrets["OMNIROUTE_API_KEY"] = "profile-b"  # pragma: allowlist secret
        self.assertEqual(self.profile.get_model_context_length("auto"), 32768)
        self.assertEqual(
            [call.args[2] for call in self.lookup.call_args_list], ["profile-a", "profile-b"]
        )

    def test_missing_key_never_probes_with_endpoint_as_credential(self):
        self.secrets.pop("OMNIROUTE_API_KEY")
        with self.assertRaisesRegex(ValueError, "scoped OmniRoute credential"):
            self.profile.get_model_context_length("auto")
        self.lookup.assert_not_called()

    def test_invalid_or_missing_endpoint_never_probes(self):
        for endpoint in (
            "",
            "http://gateway.example/v1",
            "https://user:secret@gateway.example/v1",  # pragma: allowlist secret
            "https://gateway.example/v1?token=secret",
            "https://gateway.example/other",
        ):
            with self.subTest(endpoint=endpoint):
                self.config = {}
                self.secrets["OMNIROUTE_BASE_URL"] = endpoint
                with self.assertRaises(ValueError):
                    self.profile.get_model_context_length("auto")
        self.lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
