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
TOOL = "fixture_repair"
PHASES = ("inspect", "repair", "verify")
TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL,
        "description": "Run a fixture repair phase",
        "parameters": {
            "type": "object",
            "properties": {"phase": {"type": "string", "enum": list(PHASES)}},
            "required": ["phase"],
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


def _tool_chunks(index, phase):
    return [
        (
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_fixture_{index}",
                        "type": "function",
                        "function": {"name": TOOL, "arguments": '{"phase":"'},
                    }
                ],
            },
            None,
        ),
        ({"tool_calls": [{"index": 0, "function": {"arguments": phase + '"}'}}]}, None),
        ({}, "tool_calls"),
    ]


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
    from hermes_cli.plugins import discover_plugins, get_plugin_command_handler
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
    executed = []
    expected_auth = f"Bearer {fixture_key}"
    replies = [
        *(_tool_chunks(index, phase) for index, phase in enumerate(PHASES)),
        [({"role": "assistant", "content": "Repair verified."}, None), ({}, "stop")],
        [({"role": "assistant", "content": "Hello!"}, None), ({}, "stop")],
    ]

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
            tier = "bounded" if len(decision_calls) == 1 else "routine"
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
            return httpx.Response(
                503,
                request=request,
                json={"error": {"message": "Synthetic transient failure"}},
                headers={"Retry-After": "0.01"},
            )
        return httpx.Response(
            200,
            request=request,
            content=_sse(body["model"], replies.pop(0)).encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    def execute_tool(name, args, *_args, **_kwargs):
        assert name == TOOL
        phase = args["phase"]
        executed.append(phase)
        return json.dumps({"fixture_phase": phase, "ok": phase != "inspect"})

    try:
        discover_plugins(force=True)
        command = get_plugin_command_handler("routing-classifier")
        assert command is not None
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
        ):
            assert FIRST_CLASSIFIER in command(f"select {FIRST_CLASSIFIER}")
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
            try:
                agent._cached_system_prompt = "Native routing probe."
                agent.compression_enabled = False
                repair = agent.run_conversation(
                    "Inspect this synthetic failure, apply the bounded repair and verify the result.",
                    stream_callback=lambda *_: None,
                )
                assert repair["final_response"] == "Repair verified.", repair.get(
                    "turn_exit_reason"
                )
                assert agent.model == TRIGGER
                assert SECOND_CLASSIFIER in command(f"select {SECOND_CLASSIFIER}")
                quick = agent.run_conversation(
                    "Say hello.",
                    conversation_history=repair["messages"],
                    stream_callback=lambda *_: None,
                )
                assert quick["final_response"] == "Hello!", quick.get("turn_exit_reason")
            finally:
                agent.close()
        assert executed == list(PHASES), executed
        assert [body["model"] for body in chat_calls] == [ROUTES["bounded"]] * 5 + [
            ROUTES["routine"]
        ]
        assert [decision["model"] for decision in decision_calls] == [
            FIRST_CLASSIFIER,
            SECOND_CLASSIFIER,
        ]
        assert len([call for call in http_calls if call == ("GET", "/v1/systemone/models")]) == 2
        assert len([call for call in http_calls if call == ("GET", "/v1/models")]) == 2
        assert all(body.get("tools") for body in chat_calls[:5])
        assert '"ok": false' in chat_calls[1]["messages"][-1]["content"]
        assert chat_calls[1]["messages"] == chat_calls[2]["messages"]
        assert "Say hello." in decision_calls[1]["state"]
        print(
            "NATIVE_TURN_ROUTING "
            + json.dumps(
                {
                    "mainCalls": len(chat_calls),
                    "decisions": len(decision_calls),
                    "toolCalls": len(executed),
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
