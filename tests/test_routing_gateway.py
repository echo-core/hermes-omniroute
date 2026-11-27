"""Offline contracts for scoped gateway discovery, selection and classification."""

import json
import sys
from types import SimpleNamespace

import pytest
import requests

from backend import routing_gateway as gateway

PROFILE_A = "profile-a-fixture-key"
PROFILE_B = "profile-b-fixture-key"
BASE_A = "https://gateway-a.example/v1"
BASE_B = "https://gateway-b.example/prefix/v1"
ROUTES = {"routine": "quick", "visual": "design-vision", "bounded": "repair", "escalation": "deep"}


def _catalog(*rows, more=False, cursor=None):
    return {"object": "list", "data": list(rows), "has_more": more, "last_id": cursor}


def _row(model, format="systemone", **more):
    return {"id": model, "api_format": format, **more}


def _combos(routes=ROUTES):
    return [{"id": model, "owned_by": "combo"} for model in routes.values()]


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _):
        yield json.dumps(self.body).encode()


class PluginContext:
    def __init__(self):
        self.settings = {}
        self.command = None
        self.choice = None

    def get_config(self, key):
        return self.settings.get(key)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_command(self, name, handle, **kwargs):
        self.command = handle

    def ask_choice(self, question, models):
        assert self.choice in models
        return self.choice


@pytest.fixture
def mock_http(monkeypatch):
    calls = []
    replies = []

    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, url, **kwargs):
            assert not self.trust_env
            assert kwargs["verify"] is True
            assert kwargs["allow_redirects"] is False
            assert kwargs["stream"] is True
            assert kwargs["timeout"] > 0
            calls.append((method, url, kwargs))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setitem(
        sys.modules, "agent.ssl_verify", SimpleNamespace(requests_verify=lambda: True)
    )
    return calls, replies


def _bind_profile(monkeypatch, base, key, *, chat_key=None):
    provider = SimpleNamespace(name="omniroute", runtime_base_url=lambda **kw: base)
    client = SimpleNamespace(
        api_key=chat_key or key,
        base_url=base + "/",
        auth_headers={"Authorization": f"Bearer {chat_key or key}"},
        default_headers={"Authorization": f"Bearer {chat_key or key}"},
    )
    agent = SimpleNamespace(client=client, provider="omniroute")
    monkeypatch.setitem(
        sys.modules, "providers", SimpleNamespace(get_provider_profile=lambda name: provider)
    )
    monkeypatch.setitem(
        sys.modules, "agent.web_search_provider", SimpleNamespace(get_provider_env=lambda name: key)
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.subagent_lifecycle",
        SimpleNamespace(get_active_subagent_parent=lambda: agent),
    )
    return client


def _settings(model):
    return gateway.RoutingSettings(True, "fixture-auto", model, ROUTES)


def _decision(tier, *, research=None, **metadata):
    answers = {"route": {"type": "choice", "choice": tier, "confidence": 0.9}}
    if research is not None:
        source, advanced = research
        answers["research_scope"] = {
            "type": "choice",
            "probabilities": {"source_research": source, "advanced_research": advanced},
        }
    return {"answers": answers, **metadata}


def test_discover_select_and_classify_on_each_profiles_exact_chat_identity(monkeypatch, mock_http):
    calls, replies = mock_http
    for base, key, model, tier in (
        (BASE_A, PROFILE_A, "ollama-local/tev1:0.8b", "bounded"),
        (BASE_B, PROFILE_B, "ollama-local/fixture", "visual"),
    ):
        client = _bind_profile(monkeypatch, base, key)
        ctx = PluginContext()
        ctx.settings.update({gateway.MODELS_KEY: ROUTES, gateway.ENABLED_KEY: True})
        gateway.register_selection(ctx)
        ctx.choice = model
        replies.extend(
            [
                Response(
                    _catalog(
                        _row(model),
                        _row("openai/decide", "decisions"),
                        more=True,
                        cursor="openai/decide",
                    )
                ),
                Response(_catalog(_row("provider/embed", "systemone", type="embedding"))),
                Response(_catalog(*_combos())),
                Response(_decision(tier, model=model.removeprefix("ollama-local/"))),
            ]
        )
        assert model in ctx.command("select")
        settings = gateway.RoutingSettings.from_context(ctx)
        assert settings.enabled and settings.classifier == model
        transport = gateway.Gateway.from_profile(expected_base_url=base)
        assert (
            transport.classify(settings, "Current user request: synthetic", contextual=True)
            == ROUTES[tier]
        )
        assert key not in repr(transport)
        recent = calls[-4:]
        assert [(method, url) for method, url, _ in recent] == [
            ("GET", base + "/systemone/models"),
            ("GET", base + "/systemone/models"),
            ("GET", base + "/models"),
            ("POST", base + "/systemone"),
        ]
        assert all(
            kw["headers"]["Authorization"] == client.default_headers["Authorization"]
            for _, _, kw in recent
        )
        assert recent[1][2]["params"]["after"] == "openai/decide"
        assert recent[-1][2]["json"]["model"] == model
        assert recent[-1][2]["json"]["state"] == "Current user request: synthetic"
        assert set(recent[-1][2]["json"]["questions"]["route"]["criteria"]) == set(ROUTES)


def test_live_catalog_additions_explicit_empty_catalog_and_incompatible_modes(
    monkeypatch, mock_http
):
    _, replies = mock_http
    _bind_profile(monkeypatch, BASE_A, PROFILE_A)
    ctx = PluginContext()
    gateway.register_selection(ctx)
    replies.extend(
        [
            Response(
                _catalog(_row("openai/native", "decisions"), _row("provider/embed", "embedding"))
            ),
            Response(_catalog(_row("provider/embed", "embedding"))),
            Response(_catalog()),
            Response(_catalog(_row("provider/new"))),
        ]
    )
    assert "absent" in ctx.command("select openai/native")
    assert "absent" in ctx.command("select provider/embed")
    assert "Selected provider/hidden" in ctx.command("select provider/hidden")
    listed = ctx.command("list")
    assert "provider/hidden" in listed
    assert "provider/new" in listed
    assert "https://" not in ctx.command("select https://elsewhere.example/model")
    assert ctx.settings[gateway.CLASSIFIER_KEY] == "provider/hidden"


def test_chat_key_and_endpoint_must_match_current_profile(monkeypatch, mock_http):
    calls, _ = mock_http
    client = _bind_profile(monkeypatch, BASE_A, PROFILE_A, chat_key=PROFILE_B)
    with pytest.raises(gateway.RoutingError) as wrong_key:
        gateway.Gateway.from_profile(expected_base_url=BASE_A)
    assert wrong_key.value.reason == "chat_identity_mismatch"
    assert PROFILE_A not in str(wrong_key.value)
    assert PROFILE_B not in str(wrong_key.value)
    client.api_key = PROFILE_A
    with pytest.raises(gateway.RoutingError, match="different"):
        gateway.Gateway.from_profile(expected_base_url=BASE_A)
    client.default_headers["Authorization"] = f"Bearer {PROFILE_A}"
    client.auth_headers["Authorization"] = f"Bearer {PROFILE_A}"
    with pytest.raises(gateway.RoutingError, match="different"):
        gateway.Gateway.from_profile(expected_base_url=BASE_B)
    assert calls == []


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "gateway_authentication"),
        (403, "gateway_authorization"),
        (302, "gateway_http"),
        (404, "gateway_model_unavailable"),
    ],
)
def test_decision_status_does_not_retry_or_expose_gateway_error(mock_http, status, reason):
    calls, replies = mock_http
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response({"error": PROFILE_A}, status=status, headers={"Location": BASE_B}),
        ]
    )
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    with pytest.raises(gateway.RoutingError) as failure:
        transport.classify(_settings("provider/fixture"), "Synthetic request")
    assert failure.value.reason == reason
    assert PROFILE_A not in str(failure.value)
    assert len(calls) == 2


def test_missing_combo_does_not_make_classifier_call_even_if_same_id_is_listed(mock_http):
    calls, replies = mock_http
    replies.append(Response(_catalog(*_combos()[:-1], {"id": "deep", "owned_by": "provider"})))
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    with pytest.raises(gateway.RoutingError) as failure:
        transport.classify(_settings("provider/fixture"), "Synthetic request")
    assert failure.value.reason == "answer_combo_unavailable"
    assert [method for method, *_ in calls] == ["GET"]


def test_recursive_config_or_gateway_choice_never_dispatches_answer(mock_http):
    calls, replies = mock_http
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    misconfigured = gateway.RoutingSettings(True, ROUTES["routine"], "provider/fixture", ROUTES)
    with pytest.raises(gateway.RoutingError) as failure:
        transport.classify(misconfigured, "Synthetic request")
    assert failure.value.reason == "answer_combo_configuration"
    assert calls == []
    replies.extend([Response(_catalog(*_combos())), Response(_decision("fixture-auto"))])
    with pytest.raises(gateway.RoutingError) as failure:
        transport.classify(_settings("provider/fixture"), "Synthetic request")
    assert failure.value.reason == "classifier_response"


def test_catalog_paging_validation_partial_results_and_timeout(mock_http):
    calls, replies = mock_http
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    replies.extend([Response(_catalog(more=True, cursor="same")) for _ in range(2)])
    with pytest.raises(gateway.RoutingError) as failure:
        transport.decision_models()
    assert failure.value.reason == "catalog_paging"
    assert len(calls) == 2
    replies.append(Response(_catalog(), headers={"X-OmniRoute-Catalog-Status": "partial"}))
    with pytest.raises(gateway.RoutingError) as failure:
        transport.decision_models()
    assert failure.value.reason == "catalog_incomplete"
    replies.append(requests.Timeout(PROFILE_A))
    with pytest.raises(gateway.RoutingError) as failure:
        transport.decision_models()
    assert failure.value.reason == "gateway_timeout"
    assert PROFILE_A not in str(failure.value)


def test_effective_credential_rotation_changes_identity_on_shared_gateway(monkeypatch, mock_http):
    calls, _ = mock_http
    client = _bind_profile(monkeypatch, BASE_A, PROFILE_A)
    transport = gateway.Gateway.from_profile(expected_base_url=BASE_A)
    assert transport.identity == gateway.Gateway.active_identity(expected_base_url=BASE_A)
    assert PROFILE_A.encode() not in transport.identity[1]

    client.api_key = PROFILE_B
    client.default_headers["Authorization"] = f"Bearer {PROFILE_B}"
    client.auth_headers["Authorization"] = f"Bearer {PROFILE_B}"
    assert transport.identity != gateway.Gateway.active_identity(expected_base_url=BASE_A)
    with pytest.raises(gateway.RoutingError) as failure:
        gateway.Gateway.from_profile(expected_base_url=BASE_A)
    assert failure.value.reason == "chat_identity_mismatch"
    assert calls == []


def test_scoped_bearer_can_be_supplied_by_sdk_separately_from_defaults(monkeypatch):
    client = _bind_profile(monkeypatch, BASE_A, PROFILE_A)
    client.default_headers = {"User-Agent": "synthetic-sdk"}
    transport = gateway.Gateway.from_profile(expected_base_url=BASE_A)
    assert transport.identity == gateway.Gateway.active_identity(expected_base_url=BASE_A)


@pytest.mark.parametrize(
    "default_headers,auth_headers,overrides",
    [
        ({"Authorization": object()}, {"Authorization": f"Bearer {PROFILE_A}"}, {}),
        ({"Authorization": "Basic synthetic"}, {"Authorization": f"Bearer {PROFILE_A}"}, {}),
        ({"authorization": f"Bearer {PROFILE_A}"}, {"Authorization": f"Bearer {PROFILE_A}"}, {}),
        ({}, {}, {}),
        ({"Authorization": f"Bearer {PROFILE_A}"}, {}, {}),
        ({}, {"Authorization": f"Bearer {PROFILE_A}"}, {"_api_key_provider": lambda: PROFILE_B}),
        ({}, {"Authorization": f"Bearer {PROFILE_A}"}, {"custom_auth": object()}),
        ({}, {"Authorization": f"Bearer {PROFILE_A}"}, {"_client": SimpleNamespace(auth=object())}),
    ],
)
def test_unsafe_sdk_authentication_uses_original_combo(
    monkeypatch, mock_http, default_headers, auth_headers, overrides
):
    calls, _ = mock_http
    client = _bind_profile(monkeypatch, BASE_A, PROFILE_A)
    client.default_headers = default_headers
    client.auth_headers = auth_headers
    for name, value in overrides.items():
        setattr(client, name, value)
    with pytest.raises(gateway.RoutingError) as failure:
        gateway.Gateway.from_profile(expected_base_url=BASE_A)
    assert failure.value.reason == "chat_identity_mismatch"
    assert calls == []


def test_host_without_chat_attestation_fails_closed(monkeypatch, mock_http):
    calls, _ = mock_http
    _bind_profile(monkeypatch, BASE_A, PROFILE_A)
    monkeypatch.setitem(
        sys.modules,
        "agent.subagent_lifecycle",
        SimpleNamespace(get_active_subagent_parent=lambda: None),
    )
    with pytest.raises(gateway.RoutingError) as failure:
        gateway.Gateway.from_profile(expected_base_url=BASE_A)
    assert failure.value.reason == "chat_identity_unavailable"
    assert calls == []


@pytest.mark.parametrize(
    "reported,header",
    [
        ("tev1:0.8b", None),
        ("ollama-local/tev1:0.8b", None),
        (None, "ollama-local/tev1:0.8b"),
        ("tev1:0.8b", "other:latest"),
        ("other:latest", "tev1:0.8b"),
        (None, None),
    ],
)
def test_local_decision_attests_the_selected_model(mock_http, reported, header):
    _, replies = mock_http
    response = _decision("bounded", **({"model": reported} if reported else {}))
    headers = {"X-OmniRoute-Model": header} if header else {}
    replies.extend([Response(_catalog(*_combos())), Response(response, headers=headers)])
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    settings = _settings("ollama-local/tev1:0.8b")
    if "other:latest" in (reported, header) or not (reported or header):
        with pytest.raises(gateway.RoutingError) as failure:
            transport.classify(settings, "Synthetic request")
        assert failure.value.reason == "classifier_model_mismatch"
        assert "other:latest" not in str(failure.value)
    else:
        assert transport.classify(settings, "Synthetic request") == "repair"


@pytest.mark.parametrize(
    ("second_tier", "probabilities", "expected"),
    [
        ("routine", (0.45, 0.20), "bounded"),
        ("escalation", (0.45, 0.19), "routine"),
        ("escalation", (0.10, 0.75), "bounded"),
        ("unconfigured", (0.45, 0.30), "routine"),
        ("routine", None, "routine"),
        ("routine", (True, 0.30), "routine"),
    ],
)
def test_extra_follow_up_scores_only_latest_request_and_caps_the_upgrade(
    mock_http, second_tier, probabilities, expected
):
    calls, replies = mock_http
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    model = "fixture-local:latest"
    settings = _settings(f"ollama-local/{model}")
    current = "Verify the first finding."
    contextual = f"Current request:\n{current}\n\nPrevious item:\nSynthetic finding."
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response(_decision("routine", model=model)),
            Response(_decision(second_tier, research=probabilities, model=model)),
        ]
    )
    saved = []

    def on_primary(choice):
        assert len(calls) == 2
        saved.append(choice)

    assert (
        transport.classify(
            settings,
            contextual,
            contextual=True,
            evidence_request=current,
            on_primary=on_primary,
        )
        == ROUTES[expected]
    )
    assert saved == [ROUTES["routine"]]
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", BASE_A + "/models"),
        ("POST", BASE_A + "/systemone"),
        ("POST", BASE_A + "/systemone"),
    ]
    assert all(call[2]["headers"]["Authorization"] == f"Bearer {PROFILE_A}" for call in calls)
    primary, extra = (call[2]["json"] for call in calls[1:])
    assert primary["state"] == contextual
    assert extra == {**primary, "state": current}
    assert (
        "evidence needed for the newest request"
        in extra["questions"]["research_scope"]["instructions"]
    )


@pytest.mark.parametrize(
    ("classifier", "primary_tier", "research", "is_contextual", "evidence", "cost", "expected"),
    [
        ("ollama-local/fixture", "visual", None, True, "Verify it.", None, "visual"),
        ("ollama-local/fixture", "bounded", None, True, "Verify it.", None, "bounded"),
        ("ollama-local/fixture", "escalation", None, True, "Verify it.", None, "escalation"),
        ("ollama-local/fixture", "routine", (0.7, 0), True, "Verify it.", None, "bounded"),
        ("ollama-local/fixture", "routine", None, False, "Verify it.", None, "routine"),
        ("ollama-local/fixture", "routine", None, True, None, None, "routine"),
        ("ollama-local/fixture", "routine", None, True, "Synthetic context", None, "routine"),
        ("ollama-local/fixture", "routine", None, True, "x" * 4001, None, "routine"),
        ("openrouter/fixture", "routine", None, True, "Verify it.", None, "routine"),
        ("ollama-local/fixture:cloud", "routine", None, True, "Verify it.", None, "routine"),
        ("ollama-local/fixture:0.5b-cloud", "routine", None, True, "Verify it.", None, "routine"),
        ("ollama-local/fixture", "routine", None, True, "Verify it.", 0.01, "routine"),
    ],
)
def test_extra_follow_up_only_runs_after_an_eligible_local_routine_decision(
    mock_http, classifier, primary_tier, research, is_contextual, evidence, cost, expected
):
    calls, replies = mock_http
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response(
                _decision(primary_tier, research=research, model=classifier, usage={"cost": cost})
            ),
        ]
    )
    saved = []
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    assert (
        transport.classify(
            _settings(classifier),
            "Synthetic context",
            contextual=is_contextual,
            evidence_request=evidence,
            on_primary=saved.append,
        )
        == ROUTES[expected]
    )
    assert [method for method, *_ in calls] == ["GET", "POST"]
    assert saved == []


def test_primary_callback_can_decline_the_extra_request(mock_http):
    calls, replies = mock_http
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response(_decision("routine", model="fixture-local")),
        ]
    )

    def cancelled(choice):
        assert choice == ROUTES["routine"]
        return False

    transport = gateway.Gateway(BASE_A, PROFILE_A)
    assert (
        transport.classify(
            _settings("ollama-local/fixture-local"),
            "Synthetic context",
            contextual=True,
            evidence_request="Verify the first finding.",
            on_primary=cancelled,
        )
        == ROUTES["routine"]
    )
    assert [method for method, *_ in calls] == ["GET", "POST"]


@pytest.mark.parametrize(
    "extra_reply",
    [
        requests.Timeout("synthetic timeout"),
        requests.ConnectionError("synthetic connection error"),
        Response({}, status=403),
        Response(_decision("routine", research=(0.8, 0))),
        Response(_decision("routine", research=(0.8, 0), model="other:latest")),
        Response(
            _decision("routine", research=(0.8, 0), model="fixture-local"),
            headers={"X-OmniRoute-Model": "other:latest"},
        ),
    ],
)
def test_failed_or_unattested_extra_check_keeps_the_verified_primary(mock_http, extra_reply):
    calls, replies = mock_http
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response(_decision("routine", model="fixture-local")),
            extra_reply,
        ]
    )
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    assert (
        transport.classify(
            _settings("ollama-local/fixture-local"),
            "Synthetic context",
            contextual=True,
            evidence_request="Verify the first finding.",
        )
        == ROUTES["routine"]
    )
    assert [method for method, *_ in calls] == ["GET", "POST", "POST"]


@pytest.mark.parametrize("primary_time", [0.5, 1.8])
def test_extra_follow_up_uses_only_the_remaining_budget(monkeypatch, mock_http, primary_time):
    calls, replies = mock_http
    now = 0.0
    monkeypatch.setattr(gateway, "time", SimpleNamespace(monotonic=lambda: now))

    class TimedResponse(Response):
        def __init__(self, body, duration):
            super().__init__(body)
            self.duration = duration

        def iter_content(self, chunk_size):
            nonlocal now
            now += self.duration
            yield from super().iter_content(chunk_size)

    replies.extend(
        [
            TimedResponse(_catalog(*_combos()), 0.25),
            TimedResponse(_decision("routine", model="fixture-local"), primary_time),
            TimedResponse(_decision("routine", research=(0.8, 0), model="fixture-local"), 1.5),
        ]
    )
    transport = gateway.Gateway(BASE_A, PROFILE_A)
    settings = _settings("ollama-local/fixture-local")
    if primary_time == 1.8:
        with pytest.raises(gateway.RoutingError) as failure:
            transport.classify(
                settings, "Synthetic context", contextual=True, evidence_request="Verify it."
            )
        assert failure.value.reason == "gateway_timeout"
        assert [call[2]["timeout"] for call in calls] == [2.0, 1.75]
    else:
        assert (
            transport.classify(
                settings, "Synthetic context", contextual=True, evidence_request="Verify it."
            )
            == ROUTES["routine"]
        )
        assert [call[2]["timeout"] for call in calls] == [2.0, 1.75, 1.25]


@pytest.mark.parametrize(
    ("choice", "wait", "expected"),
    [("wait", 0.70, "routine"), ("wait", 0.69, "escalation"), ("act", 0.1, "escalation")],
)
def test_local_receipt_check_preserves_primary_unless_wait_is_confident(
    mock_http, choice, wait, expected
):
    calls, replies = mock_http
    model = "fixture-local"
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response(_decision("escalation", model=model)),
            Response(
                {
                    "model": model,
                    "answers": {
                        "response": {
                            "type": "choice",
                            "choice": choice,
                            "probabilities": {"wait": wait, "act": 1 - wait},
                        }
                    },
                }
            ),
        ]
    )
    saved = []
    result = gateway.Gateway(BASE_A, PROFILE_A).classify(
        _settings("ollama-local/" + model),
        "Original context",
        contextual=True,
        receipt_check="Shall I do it?\nUnderstood.",
        on_primary=saved.append,
    )
    assert saved == [ROUTES["escalation"]]
    assert result == ROUTES[expected]
    assert set(calls[-1][2]["json"]["questions"]) == {"response"}


@pytest.mark.parametrize(
    "classifier,tier",
    [
        ("openai/fixture", "escalation"),
        ("ollama-local/fixture:cloud", "bounded"),
        ("ollama-local/fixture", "visual"),
        ("ollama-local/fixture", "routine"),
    ],
)
def test_receipt_check_only_reassesses_nonvisual_work_on_local_models(mock_http, classifier, tier):
    calls, replies = mock_http
    replies.extend(
        [Response(_catalog(*_combos())), Response(_decision(tier, model=classifier.split("/")[1]))]
    )
    result = gateway.Gateway(BASE_A, PROFILE_A).classify(
        _settings(classifier),
        "Original context",
        contextual=True,
        receipt_check="Thank you.",
    )
    assert result == ROUTES[tier]
    assert len(calls) == 2


def test_malformed_receipt_check_keeps_the_verified_first_choice(mock_http):
    _, replies = mock_http
    model = "fixture-local"
    replies.extend(
        [
            Response(_catalog(*_combos())),
            Response(_decision("bounded", model=model)),
            Response(
                {
                    "model": model,
                    "answers": {
                        "response": {
                            "type": "choice",
                            "choice": "wait",
                            "probabilities": {"wait": 0.99, "act": 0.99},
                        }
                    },
                }
            ),
        ]
    )
    saved = []
    result = gateway.Gateway(BASE_A, PROFILE_A).classify(
        _settings("ollama-local/" + model),
        "Original context",
        contextual=True,
        receipt_check="I'll wait.\nThanks.",
        on_primary=saved.append,
    )
    assert result == ROUTES["bounded"] and saved == [result]
