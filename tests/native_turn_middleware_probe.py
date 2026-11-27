"""Exercise native turn hooks and request middleware in two disposable Hermes profiles."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

NAME = "hermes-omniroute-backend"
BASE = "https://routing-fixture.invalid/v1"
TRIGGER = "auto-gpt"
ROUTES = {"routine": "quick", "visual": "seeing", "bounded": "repair", "escalation": "deep"}


def completed_batch(call_id, result):
    return [
        {"role": "user", "content": "Check the fixture."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)},
    ]


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
    # Reuse the installed interpreter's dependencies for this disposable profile.
    with (
        patch("hermes_cli.venv_sync.prepare_launch", return_value=None),
        patch("hermes_cli._early_recovery.recover_if_needed"),
        patch("pm.environments.activate_dependencies"),
    ):
        import hermes_bootstrap  # noqa: F401

    import requests
    import yaml
    from agent.context_compressor import ContextCompressor
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.turn_context import _collect_pre_llm_call_context
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.middleware import apply_llm_request_middleware
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, has_middleware
    from hermes_cli.plugins_state import save_plugin_setting
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from session_rules.turns import (
        _SUMMARY_END_MARKER,
        COMPRESSED_SUMMARY_HAS_USER_TURN_KEY,
        COMPRESSED_SUMMARY_METADATA_KEY,
        HISTORICAL_TASK_HEADING,
        MICRO_COMPACT_MARKER_KEY,
        SUMMARY_PREFIX,
    )

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
    batch_started, batch_released = Event(), Event()
    late_started, late_released, late_replied = Event(), Event(), Event()
    evidence_started, evidence_released, evidence_finished = Event(), Event(), Event()

    def endpoint(http, method, url, **kwargs):
        assert not http.trust_env and not kwargs["allow_redirects"]
        auth = kwargs["headers"]["Authorization"]
        assert auth in ("Bearer fixture-key-a", "Bearer fixture-key-b")
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
        turn_context = state.partition("\n\nCurrent tier for this task:")[0]
        _, _, latest = turn_context.partition("Latest user instruction: ")
        current = (latest or turn_context).removeprefix("Current request:\n").partition("\n")[0]
        assert (auth == "Bearer fixture-key-b") == (current == "Other profile batch")
        with lock:
            calls.append((current, body["model"], state))
        tier = {"Bad output": "invalid", "Go ahead.": "bounded"}.get(current, "routine")
        if current == "Understood." and "courtesy fixture" in state:
            tier = "escalation"
        if set(body["questions"]) == {"response"}:
            assert state.endswith("Understood.")
            assert "PRIVATE" not in state
            return Response(
                {
                    "model": body["model"].removeprefix("ollama-local/"),
                    "answers": {
                        "response": {
                            "type": "choice",
                            "choice": "wait",
                            "probabilities": {"wait": 0.9, "act": 0.1},
                        }
                    },
                }
            )
        if current == "Slow fixture":
            response_blocked.set()
            assert response_released.wait(2)
        if current == "Fixture batch" and "Most recent tool results:" in state:
            if "reported an error" in state:
                batch_started.set()
                assert batch_released.wait(2)
                tier = "bounded"
            elif "outcome unclear" in state:
                late_started.set()
                assert late_released.wait(2)
                tier = "escalation"
                late_replied.set()
        if current == "Other profile batch":
            tier = "visual" if "Most recent tool results:" in state else "escalation"
        if current == "Recheck the first finding." and state != current:
            finish(home_a, "cancel-evidence")
        if state == "Check the first finding.":
            # Hold the optional second request past the middleware's deadline.
            evidence_started.set()
            assert evidence_released.wait(3)
            evidence_finished.set()
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
        messages=None,
        expected_messages=None,
    ):
        key = client_key or f"fixture-key-{home.name}"
        client = SimpleNamespace(
            api_key=key,
            base_url=BASE + "/",
            auth_headers={"Authorization": f"Bearer {key}"},
            default_headers={},
        )
        payload = {
            "model": model,
            "messages": (
                messages
                if messages is not None
                else [{"role": "tool", "content": "synthetic result"}]
            ),
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
        expected = (
            {**payload, "messages": expected_messages} if expected_messages is not None else payload
        )
        assert {**result.payload, "model": model} == expected
        return result.payload["model"]

    def finish(home, turn, *, session="chat"):
        with bind(home):
            invoke_hook("on_session_end", session_id=session, task_id="task-" + turn, turn_id=turn)

    def snapshot_for(turn, text, history, model):
        first_call = len(calls)
        stage(home_a, turn, text, history, session=turn)
        try:
            assert dispatch(home_a, turn, session=turn) == model
        finally:
            finish(home_a, turn, session=turn)
        [(current, _, state)] = calls[first_call:]
        assert current == text
        return state

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

        assert [entry[0] for entry in calls[:-1]] == [
            "Go ahead.",
            "What is 2+2?",
            "Bad output",
            "Slow fixture",
        ]
        assert calls[-1][0].startswith("Describe the diagram")

        skill = (
            '[IMPORTANT: The user has invoked the "copy-editor" skill, indicating they want '
            "you to follow its instructions. The full skill content is loaded below.]\n"
            + "PRIVATE SKILL EXAMPLE "
            * 500
        )
        ask = (
            skill
            + "\nThe user has provided the following instruction alongside the skill invocation: rewrite this"
        )
        target = "The eastern dome closes on Monday."
        history = [
            {"role": "user", "content": "Explain the observatory schedule."},
            {"role": "assistant", "content": target},
        ]
        stage(home_a, "skill", ask, history)
        stage(home_b, "skill", ask, history)
        stage(home_a, "direct-skill", ask, history, model="direct")
        batch = completed_batch("skill-call", {"success": True})
        reminder = "PRIVATE unrelated runtime reminder"
        batch[0]["content"] = ask + "\n\n" + reminder
        batch[1]["tool_calls"][0]["function"]["name"] = "skill_view"
        separated = (
            "[Supplemental runtime context]\n"
            + reminder
            + "\n[End supplemental runtime context]\n\n"
            + ask
        )
        expected = [
            history[0],
            {**history[1], "content": routing_module._EDIT_SOURCE_LABEL + "\n" + target},
            {**batch[0], "content": separated + "\n\n" + routing_module._EDIT_SOURCE_NOTE},
            *batch[1:],
        ]
        assert dispatch(home_b, "skill", messages=history + batch) == TRIGGER
        assert (
            dispatch(home_a, "direct-skill", model="direct", messages=history + batch) == "direct"
        )
        first = len(calls)
        assert (
            dispatch(home_a, "skill", messages=history + batch[:1], expected_messages=expected[:-2])
            == "quick"
        )
        assert (
            dispatch(home_a, "skill", messages=history + batch, expected_messages=expected)
            == "quick"
        )
        assert dispatch(home_a, "skill", messages=expected) == "quick"
        assert (
            dispatch(
                home_a, "skill", messages=[*expected, {"role": "user", "content": "another turn"}]
            )
            == "quick"
        )
        states = [state for _, _, state in calls[first:]]
        assert len(states) == 2
        assert all("/copy-editor rewrite this" in state and target in state for state in states)
        assert all("PRIVATE" not in state and len(state) <= 4000 for state in states)
        plain = history + [{"role": "user", "content": ask}]
        # The same edit binding works without another plugin's context and is idempotent.
        signature = (len(ask), hashlib.sha256(ask.encode()).digest())
        layout = routing_module._skill_message_layout
        bound = layout(plain, signature)
        assert bound[-1]["content"] == ask + "\n\n" + routing_module._EDIT_SOURCE_NOTE
        assert bound[-2]["content"].endswith(target) and layout(bound, signature) == bound
        assert plain[-2]["content"] == target and plain[-1]["content"] == ask
        for preceding in (
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": target, "tool_calls": [{"id": "fixture"}]},
            {"role": "assistant", "content": target, "display_kind": "failed_turn"},
            {"role": "assistant", "content": [{"type": "text", "text": target}]},
        ):
            rows = [*history, preceding, plain[-1]]
            assert layout(rows, signature) == rows
        for instruction in ("rewrite the observatory summary", "rewrite this: use the bakery"):
            explicit = ask.removesuffix("rewrite this") + instruction
            rows = history + [{"role": "user", "content": explicit}]
            assert layout(rows, (len(explicit), hashlib.sha256(explicit.encode()).digest())) == rows
        finish(home_a, "skill")
        stage(home_a, "bare-skill", skill)
        assert dispatch(home_a, "bare-skill") == "quick"
        assert calls[-1][0] == "/copy-editor"
        finish(home_a, "bare-skill")
        caption = {"type": "text", "text": ask}
        image = {"type": "image_url", "image_url": {"url": "fixture"}}
        compact, instructed = routing_module._skill_request([caption, image])
        assert instructed and compact == [
            {"type": "text", "text": "/copy-editor rewrite this"},
            image,
        ]
        assert caption["text"] == ask

        before_receipt = len(calls)
        courtesy = [
            {"role": "system", "content": "PRIVATE SYSTEM"},
            {"role": "tool", "content": "PRIVATE TOOL"},
            {
                "role": "user",
                "content": "Design the courtesy fixture. Wait for approval.",
                "api_content": "PRIVATE RECALL",
            },
            {"role": "assistant", "content": "Shall I design the schedule?"},
        ]
        for reply in ("Okay.", "Thanks, go ahead.", "Wait until the tests pass."):
            assert routing_module._receipt_check_input(reply, courtesy) is None
        assert routing_module._receipt_check_input("Understood.", courtesy[:-1]) is None
        assert (
            routing_module._receipt_check_input("Understood.", [*courtesy, {"role": "tool"}])
            is None
        )
        older = {"role": "user", "content": "When I say I have read it, begin the work."}
        assert older["content"] in routing_module._receipt_check_input(
            "Understood.", [older, *courtesy]
        )
        stage(home_a, "receipt", "Understood.", courtesy)
        assert dispatch(home_a, "receipt") == "quick"
        assert len(calls) == before_receipt + 2
        assert "courtesy fixture" not in calls[-1][2] and "Wait for approval." in calls[-1][2]
        assert dispatch(home_a, "receipt", api_id="receipt-retry") == "quick"
        assert len(calls) == before_receipt + 2
        finish(home_a, "receipt")

        checks = (
            ("Any progress?", "The investigation is underway."),
            ("What did you find?", "An issue that needs a small code change."),
            ("Thanks.", "You're welcome! Let me know if I can help with anything else."),
        )
        for index, (ask, answer) in enumerate(checks):
            history = [
                *previous,
                {"role": "user", "content": ask},
                {"role": "assistant", "content": answer},
            ]
            state = snapshot_for(f"check-in-{index}", "Go ahead.", history, "repair")
            assert "Repair the parser regression." in state
            assert "Update the parser and verify the focused regression." in state
            assert "You're welcome!" not in state and "PRIVATE" not in state

        task = 'Review the fixture\'s polling interval and preserve "dry run" behavior.'
        snapshot = ContextCompressor._latest_user_task_snapshot([{"role": "user", "content": task}])
        assert snapshot is not None
        summary = (
            f"{SUMMARY_PREFIX}\n{HISTORICAL_TASK_HEADING}\n{snapshot}\n\n"
            f"## Generated Decisions\nPRIVATE GENERATED SUMMARY\n\n{_SUMMARY_END_MARKER}"
        )
        carrier = {
            "role": "user",
            "content": summary,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
            COMPRESSED_SUMMARY_HAS_USER_TURN_KEY: True,
        }
        compacted_tail = [
            {"role": "tool", "content": "PRIVATE COMPACTED TOOL DATA"},
            {"role": "user", "content": "Thanks."},
            {"role": "assistant", "content": "Of course."},
        ]
        state = snapshot_for("compacted", "Go ahead.", [carrier, *compacted_tail], "repair")
        assert task in state and state.count("Go ahead.") == 1
        assert SUMMARY_PREFIX not in state and HISTORICAL_TASK_HEADING not in state
        assert "PRIVATE" not in state and "Historical only" not in state

        untrusted_carriers = (
            {"role": "user", "content": summary},
            {**carrier, MICRO_COMPACT_MARKER_KEY: True},
            {**carrier, "role": "assistant"},
            {
                **carrier,
                "content": summary.replace(f"\n{HISTORICAL_TASK_HEADING}\n", "\n## Fake\n"),
            },
            {**carrier, "content": summary.replace(snapshot.splitlines()[-1], "Generated text.")},
        )
        for index, untrusted in enumerate(untrusted_carriers):
            state = snapshot_for(
                f"untrusted-compaction-{index}", "Go ahead.", [untrusted, *compacted_tail], "repair"
            )
            assert task not in state and "PRIVATE" not in state

        state = snapshot_for(
            "fresh-after-compaction", "Say hello.", [carrier, *compacted_tail], "quick"
        )
        assert task not in state and "PRIVATE" not in state

        referenced_history = [
            {"role": "user", "content": "Review the installation notes."},
            {
                "role": "assistant",
                "content": "1. The sample can hide a failed command.\n   See `example.txt`.\n2. The link moved.",
            },
        ]
        stage(home_a, "evidence-deadline", "Check the first finding.", referenced_history)
        with patch.object(routing_module, "DECISION_TIMEOUT", 0.4):
            try:
                assert dispatch(home_a, "evidence-deadline") == "quick"
                assert evidence_started.is_set() and not evidence_finished.is_set()
            finally:
                evidence_released.set()
            assert evidence_finished.wait(2)
        assert dispatch(home_a, "evidence-deadline", api_id="retry") == "quick"
        assert "1. The sample can hide a failed command." in calls[-2][2], calls[-2]
        assert "The link moved" not in calls[-2][2]
        assert calls[-1][2] == "Check the first finding."
        finish(home_a, "evidence-deadline")

        first_call = len(calls)
        closed_worker = Event()
        original_classify = routing_module.Gateway.classify

        def observe_worker(*args, **kwargs):
            try:
                return original_classify(*args, **kwargs)
            finally:
                closed_worker.set()

        with patch.object(routing_module.Gateway, "classify", observe_worker):
            stage(home_a, "cancel-evidence", "Recheck the first finding.", referenced_history)
            assert dispatch(home_a, "cancel-evidence") == TRIGGER
            assert closed_worker.wait(2)
        assert len(calls) == first_call + 1

        first_call = len(calls)
        failed_batch = completed_batch("fixture-call-1", {"exit_code": 1})
        late_batch = completed_batch("fixture-call-2", {"output": "fixture"})
        done_batch = completed_batch("fixture-call-3", {"exit_code": 0})
        stage(home_a, "batch", "Fixture batch")
        assert dispatch(home_a, "batch") == "quick"
        start_together = Barrier(3)

        def concurrent_attempt(index):
            start_together.wait(timeout=2)
            return dispatch(home_a, "batch", api_id=str(index), messages=failed_batch)

        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(concurrent_attempt, index) for index in range(3)]
            try:
                assert batch_started.wait(1)
            finally:
                batch_released.set()
            assert [future.result() for future in futures] == ["repair"] * 3
        assert len(calls) == first_call + 2

        with patch.object(routing_module, "DECISION_TIMEOUT", 0.15):
            try:
                assert dispatch(home_a, "batch", messages=late_batch) == "repair"
                assert late_started.is_set() and not late_replied.is_set()
            finally:
                late_released.set()
            assert late_replied.wait(1)
        assert dispatch(home_a, "batch", api_id="late-retry", messages=late_batch) == "repair"

        assert (
            dispatch(home_a, "batch", client_key=f"fixture-key-{home_b.name}", messages=done_batch)
            == TRIGGER
        )
        assert dispatch(home_b, "batch", messages=done_batch) == TRIGGER
        assert len(calls) == first_call + 3
        assert dispatch(home_a, "batch", messages=done_batch) == "quick"
        assert dispatch(home_a, "batch", api_id="old-retry", messages=failed_batch) == "repair"
        assert len(calls) == first_call + 4

        with bind(home_b):
            save_plugin_setting(NAME, ("turn_routing_enabled",), True)
            discover_plugins(force=True)
        stage(home_b, "batch", "Other profile batch")  # Identical task, turn and tool IDs.
        assert dispatch(home_b, "batch") == "deep"
        assert dispatch(home_b, "batch", messages=failed_batch) == "seeing"
        assert dispatch(home_a, "batch", messages=done_batch) == "quick"
        assert [entry[0] for entry in calls[first_call:]] == ["Fixture batch"] * 4 + [
            "Other profile batch"
        ] * 2
        finish(home_a, "batch")
        finish(home_b, "batch")

        with bind(home_a):
            save_plugin_setting(NAME, ("turn_routing_enabled",), False)
            discover_plugins(force=True)
        stage(home_a, "disabled", "Disabled")
        assert dispatch(home_a, "disabled") == TRIGGER

    print(
        "Native middleware probe passed for scope, concurrent batches and retries, long-chat "
        "follow-ups, compaction, veto, decision timeouts, and reload."
    )


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-omniroute-middleware-") as directory:
        root = Path(directory)
        for name in ("a", "b", "managed"):
            (root / name).mkdir()
        with (
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(root / "a"),
                    "HERMES_MANAGED_DIR": str(root / "managed"),
                    "UV_OFFLINE": "1",
                },
            ),
            patch("socket.getaddrinfo", side_effect=AssertionError("Network access during probe")),
            patch(
                "socket.socket.connect", side_effect=AssertionError("Network access during probe")
            ),
            patch(
                "socket.socket.connect_ex",
                side_effect=AssertionError("Network access during probe"),
            ),
        ):
            probe(root)


if __name__ == "__main__":
    main()
