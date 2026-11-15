"""Image guidance follows selection, credentials and the active profile scope."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guidance(monkeypatch):
    state = {"home": Path("/tmp/profile-a"), "selected": "omniroute-images", "available": True}
    images = types.ModuleType("guidance_fixture.images")
    images.PROVIDER = "omniroute-images"
    images.OmniRouteImages = lambda: types.SimpleNamespace(is_available=lambda: state["available"])
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"image_gen": {"provider": state["selected"]}}
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: state["home"]
    for name, module in {
        "guidance_fixture.images": images,
        "hermes_cli.config": config,
        "hermes_constants": constants,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "guidance_fixture.guidance", ROOT / "backend/guidance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = Mock()
    module.register(ctx)
    callback = ctx.register_system_prompt_section.call_args.args[1]
    return module, state, callback, ctx


def test_configured_backend_announces_native_roles_and_deferred_discovery(guidance):
    module, _, callback, ctx = guidance
    text = callback({})
    assert text == module.IMAGE_GUIDANCE
    assert "image_generate_complex" in text and "tool_describe" in text and "tool_call" in text
    assert "explicitly requests a different" in text
    assert "do not bypass" in text
    assert len(text) <= ctx.register_system_prompt_section.call_args.kwargs["max_chars"]
    assert "https://" not in text and "API_KEY" not in text


@pytest.mark.parametrize("provider", ["another", "", None])
def test_other_or_unset_image_provider_has_no_guidance(guidance, provider):
    _, state, callback, _ = guidance
    state["selected"] = provider
    assert callback({}) == ""


def test_lost_credential_suppresses_section_at_render_time(guidance):
    _, state, callback, _ = guidance
    assert callback({})
    state["available"] = False
    assert callback({}) == ""


def test_warm_scope_switch_cannot_render_another_profiles_section(guidance):
    _, state, callback, _ = guidance
    state["home"] = Path("/tmp/profile-b")
    assert callback({}) == ""
    state["home"] = Path("/tmp/profile-a")
    assert callback({})
