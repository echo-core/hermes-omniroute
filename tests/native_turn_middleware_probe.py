"""Exercise native turn hooks and request middleware in two disposable Hermes profiles."""

import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

NAME = "hermes-omniroute-backend"
BASE = "https://routing-fixture.invalid/v1"
TRIGGER = "auto-gpt"
ROUTES = {"routine": "quick", "visual": "seeing", "bounded": "repair", "escalation": "deep"}


class Response:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _):
        yield json.dumps(self.body).encode()


def probe(root):
    import requests
    import yaml
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.turn_context import _collect_pre_llm_call_context
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.middleware import apply_llm_request_middleware
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, has_middleware
    from hermes_cli.plugins_state import save_plugin_setting
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_a, home_b = [root / name for name in ("a", "b")]
    source = Path(__file__).resolve().parents[1]

    @contextmanager
    def bind(home):
        token = set_hermes_home_override(home)
        secret = set_secret_scope(
            {"OMNIROUTE_API_KEY": f"fixture-key-{home.name}"}, profile_home=str(home)
        )
        try:
            yield
        finally:
            reset_secret_scope(secret)
            reset_hermes_home_override(token)

    for home in (home_a, home_b):
        config = {
            "model": {"provider": "omniroute", "default": "auto", "base_url": BASE},
            "plugins": {
                "isolation": "in_process",
                "enabled": [NAME],
                "entries": {
                    NAME: {
                        "settings": {
                            "turn_routing_enabled": home == home_a,
                            "turn_routing_trigger": TRIGGER,
                            "turn_routing_classifier": "ollama-local/fixture-first",
                            "turn_routing_models": ROUTES,
                        }
                    }
                },
            },
        }
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        for package, name in (("model-provider", "hermes-omniroute-provider"), ("backend", NAME)):
            shutil.copytree(
                source / package,
                home / "plugins" / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        with bind(home):
            discover_plugins(force=True)
            assert has_middleware("llm_request") == (home == home_a)

    with bind(home_a):
        manager = get_plugin_manager()
        callback = manager._middleware["llm_request"][0]
        routing_module = sys.modules[callback.__module__]

    calls = []
    lock = Lock()
    response_blocked = Event()
    response_released = Event()

    def endpoint(http, method, url, **kwargs):
        assert not http.trust_env and not kwargs["allow_redirects"]
        auth = kwargs["headers"]["Authorization"]
        assert auth == "Bearer fixture-key-a"
        if method == "GET":
            assert url == BASE + "/models"
            return Response(
                {
                    "data": [
                        {"id": model, "owned_by": "combo", "type": "chat"}
                        for model in ROUTES.values()
                    ]
                }
            )
        assert method == "POST" and url == BASE + "/systemone"
        body = kwargs["json"]
        state = body["state"]
        current = state.removeprefix("Current request:\n").partition("\n")[0]
        with lock:
            calls.append((current, body["model"], state))
        if current == "Slow fixture":
            response_blocked.set()
            assert response_released.wait(2)
        tier = {"Bad output": "invalid", "Go ahead.": "bounded"}.get(current, "routine")
        return Response(
            {
                "model": body["model"].removeprefix("ollama-local/"),
                "answers": {"route": {"choice": tier, "confidence": 0.97}},
            }
        )

    previous = [
        {"role": "system", "content": "PRIVATE SYSTEM"},
        {
            "role": "user",
            "content": "Repair the parser regression.",
            "api_content": "PRIVATE RECALL",
        },
        {"role": "assistant", "content": "Update the parser and verify the focused regression."},
        {"role": "tool", "content": "PRIVATE TOOL"},
        {"role": "user", "content": "Thanks."},
        {"role": "assistant", "content": "Of course."},
    ]

    def stage(
        home,
        turn,
        text,
        history=(),
        *,
        session="chat",
        model=TRIGGER,
        platform="desktop",
        parent="",
        current=None,
        blocks=None,
    ):
        agent = SimpleNamespace(
            session_id=session,
            model=model,
            platform=platform,
            _parent_session_id=parent,
            _user_id=home.name,
            _persist_disabled=False,
        )
        with bind(home):
            injected = _collect_pre_llm_call_context(
                agent,
                effective_task_id="task-" + turn,
                turn_id=turn,
                original_user_message=text,
                messages=[*history, {"role": "user", "content": current or text}],
                conversation_history=list(history),
                blocks=blocks,
            )
        assert injected == ""

    def dispatch(
        home,
        turn,
        *,
        session="chat",
        api_id="api",
        model=TRIGGER,
        provider="omniroute",
        client_key=None,
        extra_headers=None,
    ):
        key = client_key or f"fixture-key-{home.name}"
        client = SimpleNamespace(
            api_key=key, base_url=BASE + "/", default_headers={"Authorization": f"Bearer {key}"}
        )
        payload = {
            "model": model,
            "messages": [{"role": "tool", "content": "synthetic result"}],
            "stream": True,
            "tools": [{"type": "function", "function": {"name": "fixture"}}],
            "reasoning_effort": "medium",
            "extra_headers": extra_headers or {"X-Fixture": "kept"},
        }
        agent = SimpleNamespace(client=client, provider=provider)
        with bind(home), bind_subagent_parent(agent):
            result = apply_llm_request_middleware(
                payload,
                task_id="task-" + turn,
                turn_id=turn,
                api_request_id=api_id,
                session_id=session,
                platform="desktop",
                model=model,
                provider=provider,
                base_url=BASE,
                api_mode="chat_completions",
                api_call_count=0,
            )
        assert result.original_payload == payload
        assert {**result.payload, "model": model} == payload
        return result.payload["model"]

    def finish(home, turn, *, session="chat"):
        with bind(home):
            invoke_hook("on_session_end", session_id=session, task_id="task-" + turn, turn_id=turn)

    with patch.object(requests.Session, "request", endpoint):
        stage(home_a, "ask", "Go ahead.", previous)
        stage(home_b, "ask", "A question in another profile.")
        assert dispatch(home_b, "ask") == TRIGGER
        assert dispatch(home_a, "auxiliary") == TRIGGER
        assert dispatch(home_a, "ask", model="manual") == "manual"
        assert dispatch(home_a, "ask", provider="other") == TRIGGER
        assert (
            dispatch(home_a, "ask", extra_headers={"Authorization": "Bearer fixture-override"})
            == TRIGGER
        )
        assert not calls

        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [
                workers.submit(dispatch, home_a, "ask", session="rotated-chat", api_id=str(idx))
                for idx in range(3)
            ]
            assert [future.result() for future in futures] == ["repair"] * 3
        [(current, classifier, state)] = calls
        assert current == "Go ahead." and classifier == "ollama-local/fixture-first"
        assert state.count("Go ahead.") == 1 and "Update the parser" in state
        assert "PRIVATE" not in state and "Of course." not in state

        with bind(home_a):
            save_plugin_setting(NAME, ("turn_routing_classifier",), "ollama-local/fixture-next")
        assert dispatch(home_a, "ask", api_id="retry") == "repair"
        assert dispatch(home_a, "ask", client_key=f"fixture-key-{home_b.name}") == TRIGGER
        assert len(calls) == 1
        finish(home_a, "ask", session="rotated-chat")
        assert dispatch(home_a, "ask") == TRIGGER

        stage(home_a, "next", "What is 2+2?", previous, session="rotated-chat")
        assert dispatch(home_a, "next", session="rotated-chat") == "quick"
        assert (
            calls[-1][1] == "ollama-local/fixture-next" and "Previous tier: bounded" in calls[-1][2]
        )
        finish(home_a, "next", session="rotated-chat")

        stage(home_a, "bad", "Bad output")
        assert dispatch(home_a, "bad") == dispatch(home_a, "bad", api_id="retry") == TRIGGER
        stage(home_a, "override", "Credential mismatch")
        assert dispatch(home_a, "override", client_key=f"fixture-key-{home_b.name}") == TRIGGER
        assert dispatch(home_a, "override") == TRIGGER
        stage(home_a, "sub", "Subagent work", platform="subagent", parent="chat")
        stage(home_a, "cron", "Scheduled work", platform="cron")
        assert dispatch(home_a, "sub") == dispatch(home_a, "cron") == TRIGGER

        def veto(**_):
            return {"action": "block", "message": "Fixture veto"}

        manager._hooks["pre_llm_call"].append(veto)
        blocks = []
        try:
            stage(home_a, "veto", "Blocked input", blocks=blocks)
            assert blocks == ["Fixture veto"]
            finish(home_a, "veto")
            assert dispatch(home_a, "veto") == TRIGGER
        finally:
            manager._hooks["pre_llm_call"].remove(veto)

        stage(home_a, "slow", "Slow fixture")
        with patch.object(routing_module, "DECISION_TIMEOUT", 0.15):
            started = time.monotonic()
            assert dispatch(home_a, "slow") == TRIGGER
            assert time.monotonic() - started < 0.8 and response_blocked.is_set()
            response_released.set()
            assert dispatch(home_a, "slow", api_id="retry") == TRIGGER

        stage(
            home_a,
            "visual",
            "Describe the diagram " + "x" * 4500,
            previous,
            current=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_IMAGE"}},
                {"type": "text", "text": "PRIVATE API ENRICHMENT"},
            ],
        )
        assert dispatch(home_a, "visual") == "quick"  # The fixture chooses by exact request.
        visual_state = calls[-1][2]
        assert len(visual_state) == 4000 and "Visual input is present" in visual_state
        assert "PRIVATE" not in visual_state and visual_state.startswith(
            "Current request:\nDescribe"
        )
        finish(home_a, "visual")

        with bind(home_a):
            save_plugin_setting(NAME, ("turn_routing_enabled",), False)
            discover_plugins(force=True)
        stage(home_a, "disabled", "Disabled")
        assert dispatch(home_a, "disabled") == TRIGGER
        assert [entry[0] for entry in calls[:-1]] == [
            "Go ahead.",
            "What is 2+2?",
            "Bad output",
            "Slow fixture",
        ]
        assert calls[-1][0].startswith("Describe the diagram")

    print(
        "Native middleware probe passed for scope, retries, compaction IDs, veto, timeout, and reload."
    )


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-omniroute-middleware-") as directory:
        root = Path(directory)
        for name in ("a", "b", "managed"):
            (root / name).mkdir()
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(root / "a"),
                "HERMES_MANAGED_DIR": str(root / "managed"),
            },
        ):
            probe(root)


if __name__ == "__main__":
    main()
