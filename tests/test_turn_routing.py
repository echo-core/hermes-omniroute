"""Routing admission, registration compatibility, and idle cleanup."""

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


def test_abandoned_stage_retimes_idle_cleanup_and_ignores_obsolete_callback(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(turn_routing, "time", SimpleNamespace(monotonic=lambda: now))
    long_timer, staged_timer, replacement = Mock(), Mock(), Mock()
    timer_factory = Mock(side_effect=[long_timer, staged_timer, replacement])
    monkeypatch.setattr(turn_routing, "Timer", timer_factory)
    router = turn_routing._Router(ctx=None, home="fixture")
    active = SimpleNamespace(expires=now + turn_routing.TURN_TTL, close=Mock())
    router.turns["active"] = active
    router._schedule_expiration(now)
    delay, obsolete_callback = timer_factory.call_args.args
    assert delay == turn_routing.TURN_TTL
    long_timer.start.assert_called_once()

    staged = SimpleNamespace(expires=now + turn_routing.STAGED_TTL, close=Mock())
    router.turns["staged"] = staged
    router._schedule_expiration(now)
    delay, staged_callback = timer_factory.call_args.args
    assert delay == turn_routing.STAGED_TTL
    long_timer.cancel.assert_called_once()
    staged_timer.start.assert_called_once()

    now = staged.expires
    obsolete_callback()
    staged.close.assert_not_called()
    assert router.expiration == (staged.expires, staged_timer)

    staged_callback()
    staged.close.assert_called_once()
    active.close.assert_not_called()
    assert router.turns == {"active": active}
    assert router.expiration == (active.expires, replacement)
    replacement.start.assert_called_once()
