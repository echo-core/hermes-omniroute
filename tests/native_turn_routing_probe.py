"""Run Hermes's native conversation loop against a synthetic HTTPS gateway.

Run with the Hermes runtime and installed source on the import path. The probe
installs both working-tree plugin packages into a disposable profile and blocks
real Python network connections.
"""

import io
import json
import os
import shutil
import tempfile
from collections import deque
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

BASE = "https://routing-fixture.example/v1"
TRIGGER = "auto-gpt"
ROUTES = {
    "routine": "gpt-6-luna",
    "visual": "gpt-6-terra",
    "bounded": "gpt-6-sol",
    "escalation": "gpt-6-astra",
}
FIRST_CLASSIFIER = "ollama-local/fixture-one"
SECOND_CLASSIFIER = "ollama-local/fixture-two"
TOOL = "terminal"
PRIVATE_ARGUMENT = "PRIVATE_FIXTURE_ARGUMENT"
PRIVATE_OUTPUT = "PRIVATE_FIXTURE_OUTPUT"
INSPECT = f"check parser fixture {PRIVATE_ARGUMENT}"
REPAIR = "apply parser fixture repair"
VERIFY = "verify parser fixture"
TOOL_RESULTS = {
    INSPECT: {"exit_code": 1, "status": "error"},
    REPAIR: {"exit_code": 0, "status": "success"},
    VERIFY: {"exit_code": 0, "status": "success"},
}
TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL,
        "description": "Run a fixture repair phase",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "enum": list(TOOL_RESULTS)}},
            "required": ["command"],
        },
    },
}


def _sse(model, chunks):
    return (
        "".join(
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "\n\n"
            for delta, finish in chunks
        )
        + "data: [DONE]\n\n"
    )


def _tool_chunks(index, command):
    return [
        (
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_fixture_{index}",
                        "type": "function",
                        "function": {"name": TOOL, "arguments": '{"command":'},
                    }
                ],
            },
            None,
        ),
        (
            {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(command) + "}"}}]},
            None,
        ),
        ({}, "tool_calls"),
    ]


def _text_chunks(answer):
    return [({"role": "assistant", "content": answer}, None), ({}, "stop")]


def _probe(root):
    # The installed interpreter already has the runtime's dependencies. Keep
    # launcher repair and package synchronization out of the test profile.
    with (
        patch("hermes_cli.venv_sync.prepare_launch", return_value=None),
        patch("hermes_cli._early_recovery.recover_if_needed"),
        patch("pm.environments.activate_dependencies"),
    ):
        import hermes_bootstrap  # noqa: F401

    import httpx
    import requests
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli.plugins import discover_plugins, get_plugin_command_handler, get_plugin_manager
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from run_agent import AIAgent

    home = root / "home"
    fixture_key = "native-routing-fixture-key"  # pragma: allowlist secret
    token = set_hermes_home_override(home)
    secret = set_secret_scope({"OMNIROUTE_API_KEY": fixture_key}, profile_home=str(home))
    http_calls = []
    decision_calls = []
    chat_calls = []
    native_attempts = []
    executed = []
    expected_auth = f"Bearer {fixture_key}"
    replies = deque()
    predictions = deque()
    sequences = {}

    def send_gateway(_session, request, **kwargs):
        path = urlsplit(request.url).path
        assert request.url.split("/v1/")[0] == BASE.removesuffix("/v1")
        assert request.headers["Authorization"] == expected_auth
        http_calls.append((request.method, path))
        if path == "/v1/systemone/models":
            body = {
                "data": [
                    {"id": model, "api_format": "systemone"}
                    for model in (FIRST_CLASSIFIER, SECOND_CLASSIFIER)
                ]
            }
        elif path == "/v1/models":
            body = {"data": [{"id": model, "owned_by": "combo"} for model in ROUTES.values()]}
        elif path == "/v1/systemone":
            decision = json.loads(request.body)
            decision_calls.append(decision)
            tier = predictions.popleft()
            body = {
                "model": decision["model"].removeprefix("ollama-local/"),
                "answers": {"route": {"type": "choice", "choice": tier, "confidence": 0.99}},
            }
        else:
            raise AssertionError(f"Unexpected fixture endpoint {path}")
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response.raw = io.BytesIO(json.dumps(body).encode())
        response.request = request
        return response

    def send_chat(_client, request, **kwargs):
        assert str(request.url) == BASE + "/chat/completions"
        assert request.headers["Authorization"] == expected_auth
        body = json.loads(request.content)
        assert kwargs.get("stream") is True and body.get("stream") is True
        chat_calls.append(body)
        if len(chat_calls) == 2:
            # A 408 exercises the outer retry without invoking the separate 5xx streaming probe.
            return httpx.Response(
                408,
                request=request,
                json={"error": {"message": "Synthetic transient failure"}},
                headers={"Retry-After": "0.01"},
            )
        return httpx.Response(
            200,
            request=request,
            content=_sse(body["model"], replies.popleft()).encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    def execute_tool(name, args, *_args, **_kwargs):
        assert name == TOOL
        command = args["command"]
        executed.append(command)
        return json.dumps({**TOOL_RESULTS[command], "output": (PRIVATE_OUTPUT + " ") * 20})

    def observe_attempt(api_request_id, retry_count, request):
        native_attempts.append((api_request_id, retry_count, request.get("body", {}).get("model")))

    def make_agent(runtime):
        agent = AIAgent(
            provider=runtime["provider"],
            api_mode=runtime["api_mode"],
            base_url=runtime["base_url"],
            api_key=runtime["api_key"],
            model=TRIGGER,
            max_iterations=8,
            platform="cli",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            cwd=str(home),
        )
        agent._cached_system_prompt = "Native routing probe."
        agent.compression_enabled = False
        return agent

    def run_turn(agent, ask, answer, tiers, *, tools=(), history=(), retries=0, routed_ask=None):
        assert len(tiers) == len(tools) + 1
        first_chat, first_decision = len(chat_calls), len(decision_calls)
        first_tool = len(executed)
        replies.extend(_tool_chunks(first_tool + idx, tool) for idx, tool in enumerate(tools))
        replies.append(_text_chunks(answer))
        predictions.extend(tiers)
        result = agent.run_conversation(
            ask, conversation_history=list(history), stream_callback=lambda *_: None
        )
        assert result["final_response"] == answer, result.get("turn_exit_reason")
        assert agent.model == TRIGGER and not replies
        assert not predictions, f"Missing {len(predictions)} classifier calls for {ask!r}"
        assert executed[first_tool:] == list(tools)
        chats = chat_calls[first_chat:]
        decisions = decision_calls[first_decision:]
        assert len(chats) == len(tiers) + retries and len(decisions) == len(tiers)
        assert all(body.get("tools") for body in chats)
        assert all((routed_ask or ask) in decision["state"] for decision in decisions)
        return result["messages"], [body["model"] for body in chats], decisions

    try:
        discover_plugins(force=True)
        command = get_plugin_command_handler("routing-classifier")
        assert command is not None
        get_plugin_manager()._hooks.setdefault("pre_api_request", []).append(observe_attempt)
        runtime = resolve_runtime_provider(requested="omniroute", target_model=TRIGGER)
        with (
            patch.object(httpx.Client, "send", send_chat),
            patch.object(requests.Session, "send", send_gateway),
            patch("agent.title_generator.maybe_auto_title"),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
            patch(
                "agent.model_metadata._resolve_custom_endpoint_context_length", return_value=64_000
            ),
            patch("model_tools.get_tool_definitions", return_value=[TOOL_SCHEMA]),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("model_tools.handle_function_call", side_effect=execute_tool),
            patch("agent.turn_api_error.compute_error_backoff", return_value=0),
        ):
            assert FIRST_CLASSIFIER in command(f"select {FIRST_CLASSIFIER}")
            agent = make_agent(runtime)
            try:
                repair_ask = "Can you take a look?"
                repair_answer = (
                    "The parser dropped the last field. I fixed the field boundary and the focused "
                    "regression check passed. I can document the cause and the verification steps."
                )
                history, sequences["repair"], repair_decisions = run_turn(
                    agent,
                    repair_ask,
                    repair_answer,
                    ["routine", "bounded", "bounded", "bounded"],
                    tools=(INSPECT, REPAIR, VERIFY),
                    retries=1,
                )
                assert sequences["repair"] == [ROUTES["routine"]] + [ROUTES["bounded"]] * 4
                assert all(decision["model"] == FIRST_CLASSIFIER for decision in repair_decisions)

                assert SECOND_CLASSIFIER in command(f"select {SECOND_CLASSIFIER}")
                # Fifty exchanges put the committed plan outside the old 96-message window.
                acknowledgments = ("Thanks.", "Great.", "Sounds good.", "Okay.", "Got it.")
                for acknowledgment in acknowledgments * 10:
                    history, models, _ = run_turn(
                        agent, acknowledgment, "Of course.", ["routine"], history=history
                    )
                    assert models == [ROUTES["routine"]]
                assert sum(message["role"] == "user" for message in history) == 51

                history, sequences["continuation"], continuation = run_turn(
                    agent,
                    "Go ahead.",
                    "Documented the field boundary fix and the regression check.",
                    ["bounded"],
                    history=history,
                )
                assert sequences["continuation"] == [ROUTES["bounded"]]
                assert repair_ask in continuation[0]["state"]
                assert "I can document the cause" in continuation[0]["state"]
                assert "Of course." not in continuation[0]["state"]

                history, sequences["independent"], independent = run_turn(
                    agent, "Say hello.", "Hello!", ["routine"], history=history
                )
                assert sequences["independent"] == [ROUTES["routine"]]
                assert "Previous tier: bounded" in independent[0]["state"]

                _, sequences["status"], status_decisions = run_turn(
                    agent,
                    "Check whether the fixture is ready. A short status is fine.",
                    "Ready.",
                    ["routine", "bounded", "routine"],
                    tools=(INSPECT, VERIFY),
                    history=history,
                )
                assert sequences["status"] == [ROUTES[t] for t in ("routine", "bounded", "routine")]
            finally:
                agent.close()

            fresh = make_agent(runtime)
            try:
                assert fresh.session_id != agent.session_id
                _, sequences["fresh"], fresh_decisions = run_turn(
                    fresh, "Say hello.", "Hello!", ["routine"]
                )
                assert sequences["fresh"] == [ROUTES["routine"]]
                assert fresh_decisions[0]["state"] == "Say hello."
                reminder = "Additional runtime guidance supplied by another fixture plugin."
                get_plugin_manager()._hooks["pre_llm_call"].append(lambda **_: reminder)
                skill = (
                    '[IMPORTANT: The user has invoked the "copy-editor" skill, indicating they want '
                    "you to follow its instructions. The full skill content is loaded below.]\n"
                    + "Style example. " * 600
                    + "\nThe user has provided the following instruction alongside the skill invocation: rewrite this"
                )
                target = "The two public telescopes will reopen tomorrow."
                first = len(chat_calls)
                _, _, skill_decisions = run_turn(
                    fresh,
                    skill,
                    "The telescopes reopen tomorrow.",
                    ["bounded", "routine"],
                    history=[
                        {"role": "user", "content": "When can we visit?"},
                        {"role": "assistant", "content": target},
                    ],
                    tools=(VERIFY,),
                    routed_ask="/copy-editor rewrite this",
                )
                for body in chat_calls[first:]:
                    user = next(m for m in reversed(body["messages"]) if m["role"] == "user")
                    assert "entire preceding assistant answer" in user["content"].split(skill)[1]
                    prior = body["messages"][body["messages"].index(user) - 1]
                    assert prior["role"] == "assistant" and prior["content"].endswith(target)
                    assert prior["content"].startswith("[Source passage")
                    assert target not in user["content"]
                    assert user["content"].count(reminder) == 1
                    assert user["content"].index(reminder) < user["content"].index(skill)
                for decision in skill_decisions:
                    assert target in decision["state"] and reminder not in decision["state"]

            finally:
                fresh.close()

        assert all(decision["model"] == SECOND_CLASSIFIER for decision in decision_calls[4:])
        assert len([call for call in http_calls if call == ("GET", "/v1/systemone/models")]) == 2
        assert json.loads(chat_calls[1]["messages"][-1]["content"])["exit_code"] == 1
        assert chat_calls[1] == chat_calls[2]
        assert len(native_attempts) == len(chat_calls)
        assert native_attempts[1][0] == native_attempts[2][0]
        assert [attempt[1] for attempt in native_attempts[1:3]] == [0, 1]
        assert [attempt[2] for attempt in native_attempts[1:3]] == [ROUTES["bounded"]] * 2
        for decision in decision_calls:
            state = decision["state"]
            assert len(state) <= 4000
            assert PRIVATE_OUTPUT not in state and PRIVATE_ARGUMENT not in state
        for group in (repair_decisions, status_decisions):
            assert "Most recent tool results:" not in group[0]["state"]
            for previous_tier, decision in zip(("routine", "bounded", "bounded"), group[1:]):
                state = decision["state"]
                assert f"Current tier for this task: {previous_tier}\n" in state
                assert "Most recent tool results:" in state
                assert "Work the user requested:" in state
            assert "command execution (reported an error)" in group[1]["state"]
            assert "command execution (completed)" in group[-1]["state"]
        print(
            "NATIVE_TURN_ROUTING "
            + json.dumps(
                {
                    "mainCalls": len(chat_calls),
                    "decisions": len(decision_calls),
                    "toolCalls": len(executed),
                    "nativeRetries": 1,
                    "routes": sequences,
                    "providerCalls": 0,
                }
            )
        )
    finally:
        reset_secret_scope(secret)
        reset_hermes_home_override(token)


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-native-turn-routing-") as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        managed = root / "managed"
        managed.mkdir()
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_MANAGED_DIR"] = str(managed)
        settings = {"turn_routing_enabled": True, "turn_routing_models": ROUTES}
        (home / "config.yaml").write_text(
            json.dumps(
                {
                    "model": {
                        "provider": "omniroute",
                        "api_mode": "chat_completions",
                        "base_url": BASE,
                        "default": TRIGGER,
                    },
                    "plugins": {
                        "isolation": "in_process",
                        "enabled": ["hermes-omniroute-backend", "hermes-omniroute-provider"],
                        "entries": {"hermes-omniroute-backend": {"settings": settings}},
                    },
                }
            ),
            encoding="utf-8",
        )
        checkout = Path(__file__).resolve().parents[1]
        for directory, name in (("backend", "backend"), ("model-provider", "provider")):
            shutil.copytree(
                checkout / directory,
                home / "plugins" / f"hermes-omniroute-{name}",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=AssertionError("Internet access during native probe"),
            ),
            patch(
                "socket.socket.connect",
                side_effect=AssertionError("Network connection during native probe"),
            ),
            patch(
                "socket.socket.connect_ex",
                side_effect=AssertionError("Network connection during native probe"),
            ),
        ):
            _probe(root)


if __name__ == "__main__":
    main()
