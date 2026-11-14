import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def plugin(monkeypatch):
    def module(name, **values):
        value = types.ModuleType(name)
        value.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, value)

    module(
        "agent.image_gen_provider",
        ImageGenProvider=object,
        error_response=lambda **kw: dict(success=False, **kw),
        success_response=lambda **kw: dict(success=True, **kw),
        resolve_aspect_ratio=lambda value: value,
    )
    module("agent.secret_scope", get_secret=lambda key: "synthetic-fixture")
    module(
        "plugins.image_gen._common",
        collect_source_images=lambda primary, refs, limit: (
            ([primary] if primary else []) + (refs or [])
        ),
        materialize_image=lambda *a, **kw: ("/tmp/synthetic.png", None),
        record_token_usage=Mock(),
        size_for=lambda aspect: "1024x1024",
    )
    opened = []

    def open_image(ref):
        file = io.BytesIO(b"synthetic-image")
        opened.append(file)
        return file

    module("plugins.image_gen.openai", _named_bytes_io=open_image)

    class RateLimitError(Exception):
        status_code = 429

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    response = types.SimpleNamespace(
        data=[types.SimpleNamespace(b64_json="fixture", url=None)], usage=None
    )
    client.images.generate.return_value = response
    client.images.edit.return_value = response
    factory = Mock(return_value=client)
    module("openai", OpenAI=factory, RateLimitError=RateLimitError)
    source = Path(__file__).parents[1] / "backend/images.py"
    spec = importlib.util.spec_from_file_location("image_modes_fixture", source)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    cfg = {"base_url": "https://gateway.example/v1", "timeout": 180}
    monkeypatch.setattr(value, "settings", lambda: cfg)
    return value, client, factory, RateLimitError, cfg, opened


@pytest.mark.parametrize(
    "mode,expected", [("regular", "gpt-image-2.5-flare"), ("complex", "gpt-image-2.5-sunburst")]
)
def test_mode_selects_exact_gateway_model(plugin, mode, expected):
    value, client, factory, _, _, _ = plugin
    result = value.OmniRouteImages().generate("Synthetic", mode=mode)
    assert result["success"]
    assert client.images.generate.call_args.kwargs["model"] == "codex/" + expected
    assert factory.call_args.kwargs["max_retries"] == 0
    assert client.images.generate.call_args.kwargs["timeout"] <= 180


def test_regular_fallback_only_after_definite_rate_limit(plugin):
    value, client, _, RateLimitError, _, _ = plugin
    response = client.images.generate.return_value
    client.images.generate.side_effect = [RateLimitError(), response]
    result = value.OmniRouteImages().generate("Synthetic")
    assert result["success"]
    assert result["extra"]["attempted_models"] == [value.REGULAR, value.COMPLEX]


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("ambiguous failure")])
def test_ambiguous_failure_does_not_duplicate_job(plugin, error):
    value, client, *_ = plugin
    client.images.generate.side_effect = error
    assert not value.OmniRouteImages().generate("Synthetic")["success"]
    assert client.images.generate.call_count == 1


def test_complex_never_downgrades(plugin):
    value, client, _, RateLimitError, _, _ = plugin
    client.images.generate.side_effect = RateLimitError()
    assert not value.OmniRouteImages().generate("Synthetic", mode="complex")["success"]
    assert client.images.generate.call_count == 1


def test_local_edits_upload_bytes_and_close_files(plugin):
    value, client, _, _, _, opened = plugin
    assert value.OmniRouteImages().generate("Synthetic", image_url="/tmp/source.png")["success"]
    assert isinstance(client.images.edit.call_args.kwargs["image"], io.BytesIO)
    assert opened[0].closed
    client.images.generate.assert_not_called()


def test_configured_role_ids_are_respected(plugin):
    value, client, _, _, cfg, _ = plugin
    cfg["complex_model"] = "codex/explicit-complex"
    value.OmniRouteImages().generate("Synthetic", mode="complex")
    assert client.images.generate.call_args.kwargs["model"] == "codex/explicit-complex"


def test_invalid_endpoint_never_uses_default_paid_api(plugin):
    value, _, factory, _, cfg, _ = plugin
    cfg["base_url"] = ""
    assert value.OmniRouteImages().generate("Synthetic")["error_type"] == "configuration_error"
    factory.assert_not_called()


def test_registration_adds_complex_without_overriding_builtin(plugin):
    value, *_ = plugin
    ctx = Mock()
    value.register(ctx)
    registration = ctx.register_tool.call_args.kwargs
    assert registration["name"] == "image_generate_complex"
    assert registration.get("override", False) is False
    assert registration["schema"]["parameters"]["required"] == ["prompt"]
