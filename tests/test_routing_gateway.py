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


def _decision(tier, **metadata):
    return {"answers": {"route": {"type": "choice", "choice": tier, "confidence": 0.9}}, **metadata}


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
    assert transport.identity != gateway.Gateway.active_identity(expected_base_url=BASE_A)
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
