"""Routing admission preserves older and default-off backend registration."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import backend
from backend import turn_routing


def test_legacy_backend_context_keeps_image_and_web_registration(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "backend.guidance",
        SimpleNamespace(register=lambda ctx: calls.append("guidance")),
    )
    monkeypatch.setitem(
        sys.modules, "backend.images", SimpleNamespace(register=lambda ctx: calls.append("images"))
    )
    monkeypatch.setitem(
        sys.modules, "backend.web", SimpleNamespace(OmniRouteWebProvider=lambda: "web")
    )
    ctx = SimpleNamespace(register_web_search_provider=calls.append)
    backend.register(ctx)
    assert calls == ["images", "web", "guidance"]


def test_selector_available_with_routing_off():
    ctx = Mock()
    ctx.get_config.return_value = None
    turn_routing.register(ctx)
    assert ctx.register_command.call_args.args[0] == "routing-classifier"
    ctx.register_hook.assert_not_called()
    ctx.register_middleware.assert_not_called()


def test_enabled_on_older_context_without_middleware_keeps_selector():
    register_command = Mock()
    register_hook = Mock()
    ctx = SimpleNamespace(
        get_config=lambda key: key == "turn_routing_enabled",
        set_config=Mock(),
        register_command=register_command,
        register_hook=register_hook,
    )
    turn_routing.register(ctx)
    assert register_command.call_args.args[0] == "routing-classifier"
    register_hook.assert_not_called()
