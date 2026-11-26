"""Profile-scoped OmniRoute decision discovery and System One requests."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
from urllib.parse import urlsplit

from .routing_policy import MAX_PROMPT_CHARS, build_questions, select_route

ENABLED_KEY = "turn_routing_enabled"
TRIGGER_KEY = "turn_routing_trigger"
CLASSIFIER_KEY = "turn_routing_classifier"
MODELS_KEY = "turn_routing_models"
TIERS = ("routine", "visual", "bounded", "escalation")
DEFAULT_TRIGGER = "auto-gpt"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 512 * 1024
CATALOG_TIMEOUT_S = 5.0
_COMBO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]*(?:/[A-Za-z0-9@][A-Za-z0-9_.:@-]*)*")


class RoutingError(Exception):
    """Fixed diagnostic safe for logs or the selection command."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def _model_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 200 and bool(_COMBO_ID.fullmatch(value))


def _classifier_id(value: object) -> bool:
    return _model_id(value) and "/" in value


@dataclass(frozen=True)
class RoutingSettings:
    enabled: bool
    trigger: str
    classifier: str
    models: Mapping[str, str]

    @classmethod
    def from_context(cls, ctx):
        return cls.from_config(
            {
                key: ctx.get_config(key)
                for key in (ENABLED_KEY, TRIGGER_KEY, CLASSIFIER_KEY, MODELS_KEY)
            }
        )

    @classmethod
    def from_config(cls, config):
        enabled = config.get(ENABLED_KEY) is True
        trigger = config.get(TRIGGER_KEY)
        if trigger is None:
            trigger = DEFAULT_TRIGGER
        classifier = config.get(CLASSIFIER_KEY)
        raw_models = config.get(MODELS_KEY)
        models = (
            {
                tier: value
                for tier, value in raw_models.items()
                if tier in TIERS and _model_id(value)
            }
            if isinstance(raw_models, Mapping)
            else {}
        )
        return cls(
            enabled=enabled,
            trigger=trigger if _model_id(trigger) else "",
            classifier=classifier if _classifier_id(classifier) else "",
            models=models,
        )


def _gateway_url(value: object) -> str:
    if isinstance(value, str):
        base = value.strip().rstrip("/")
        try:
            url = urlsplit(base)
            port = url.port
            if (
                url.scheme == "https"
                and url.hostname
                and (port is None or port > 0)
                and url.path.endswith("/v1")
                and url.username is None
                and url.password is None
                and "?" not in base
                and "#" not in base
                and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in base)
            ):
                return base
        except ValueError:
            pass
    raise RoutingError("gateway_url", "Configure a trusted OmniRoute HTTPS endpoint ending in /v1.")


def _configured_gateway():
    from agent.web_search_provider import get_provider_env
    from providers import get_provider_profile

    profile = get_provider_profile("omniroute")
    key = get_provider_env("OMNIROUTE_API_KEY")
    if profile is None or profile.name != "omniroute":
        raise RoutingError("gateway_provider", "Enable the native OmniRoute model provider.")
    if not key or not key.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in key):
        raise RoutingError("gateway_credential", "The current profile has no usable OmniRoute key.")
    endpoint = profile.runtime_base_url(api_key=key, default="", env_url="")
    return _gateway_url(endpoint), key


def _identity(base_url: str, key: str) -> tuple[str, bytes]:
    return base_url, sha256(key.encode()).digest()


def _uses_scoped_bearer(client, key: str) -> bool:
    # Chat Completions adds the SDK bearer separately from default_headers in
    # newer SDKs. Older SDKs already include that same header in the defaults.
    auth = client.auth_headers
    defaults = client.default_headers
    if not isinstance(auth, Mapping) or not isinstance(defaults, Mapping):
        return False
    authorization = {
        name: value for name, value in auth.items() if name.casefold() == "authorization"
    }
    if authorization != {"Authorization": f"Bearer {key}"}:
        return False
    merged = {**authorization, **defaults}
    wire_auth = [val for name, val in merged.items() if name.casefold() == "authorization"]
    return (
        wire_auth == [f"Bearer {key}"]
        and getattr(client, "_api_key_provider", None) is None
        and getattr(client, "custom_auth", None) is None
        and getattr(getattr(client, "_client", None), "auth", None) is None
    )


def _active_chat(expected_base_url: str) -> tuple[str, bytes]:
    # Hermes 0.23.4 binds the effective agent across its in-process main turn.
    # A separate plugin host has no such binding and must pass through.
    from agent.subagent_lifecycle import get_active_subagent_parent

    agent = get_active_subagent_parent()
    client = getattr(agent, "client", None)
    key = getattr(client, "api_key", None)
    if not isinstance(key, str) or not key or not key.isascii():
        raise RoutingError(
            "chat_identity_unavailable", "The active chat credential is unavailable."
        )
    base_url = _gateway_url(expected_base_url)
    if (
        getattr(agent, "provider", None) != "omniroute"
        or _gateway_url(str(getattr(client, "base_url", ""))) != base_url
        or not _uses_scoped_bearer(client, key)
    ):
        raise RoutingError(
            "chat_identity_mismatch", "The active chat uses different OmniRoute credentials."
        )
    return _identity(base_url, key)


def _http_error(status: int) -> RoutingError:
    if status == 401:
        return RoutingError(
            "gateway_authentication", "OmniRoute rejected this profile’s authentication (401)."
        )
    if status == 403:
        return RoutingError(
            "gateway_authorization", "OmniRoute denied this profile’s access (403)."
        )
    if status in (400, 404, 422):
        return RoutingError(
            "gateway_model_unavailable",
            f"OmniRoute could not serve this endpoint or model ({status}).",
        )
    return RoutingError("gateway_http", f"OmniRoute returned HTTP {status}.")


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise RoutingError("gateway_timeout", "The OmniRoute request timed out.")
    return seconds


@dataclass(frozen=True)
class Gateway:
    base_url: str
    _key: str = field(repr=False)

    @classmethod
    def from_profile(cls, *, expected_base_url: str | None = None):
        try:
            base_url, key = _configured_gateway()
            if expected_base_url is not None:
                active_url, active_digest = cls.active_identity(expected_base_url=expected_base_url)
                if base_url != active_url or not compare_digest(
                    _identity(base_url, key)[1], active_digest
                ):
                    raise RoutingError(
                        "chat_identity_mismatch",
                        "The active chat uses different OmniRoute credentials.",
                    )
        except RoutingError:
            raise
        except Exception:
            raise RoutingError(
                "gateway_configuration",
                "The current profile’s OmniRoute configuration is unavailable.",
            ) from None
        return cls(base_url, key)

    @property
    def identity(self) -> tuple[str, bytes]:
        return _identity(self.base_url, self._key)

    @classmethod
    def active_identity(cls, *, expected_base_url: str) -> tuple[str, bytes]:
        try:
            return _active_chat(expected_base_url)
        except RoutingError:
            raise
        except Exception:
            raise RoutingError(
                "chat_identity_unavailable", "The active chat credential is unavailable."
            ) from None

    def _request(self, method, path, deadline, *, params=None, payload=None):
        import requests
        from agent.ssl_verify import requests_verify

        try:
            with requests.Session() as session:
                # Avoid implicit netrc authentication replacing the scoped bearer key.
                session.trust_env = False
                with session.request(
                    method,
                    self.base_url + path,
                    params=params,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
                    timeout=_remaining(deadline),
                    allow_redirects=False,
                    stream=True,
                    verify=requests_verify(),
                ) as response:
                    if response.status_code != 200:
                        raise _http_error(response.status_code)
                    raw = bytearray()
                    for chunk in response.iter_content(16 * 1024):
                        _remaining(deadline)
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise RoutingError(
                                "gateway_response_size", "The OmniRoute response is too large."
                            )
                    _remaining(deadline)
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("error"):
                        raise ValueError
                    return data, response.headers
        except requests.Timeout:
            raise RoutingError("gateway_timeout", "The OmniRoute request timed out.") from None
        except requests.RequestException:
            raise RoutingError(
                "gateway_connection", "Could not reach the configured OmniRoute gateway."
            ) from None
        except (UnicodeError, ValueError, RecursionError):
            raise RoutingError(
                "gateway_response", "OmniRoute returned an invalid response."
            ) from None

    def _catalog_pages(self, path: str, deadline: float):
        params = {"limit": PAGE_SIZE}
        seen = set()
        for _ in range(MAX_PAGES):
            data, headers = self._request("GET", path, deadline, params=params)
            status = headers.get("X-OmniRoute-Catalog-Status")
            if status not in (None, "complete") or headers.get("X-OmniRoute-Catalog") in (
                "last-good",
                "stale",
                "build-timeout",
            ):
                raise RoutingError(
                    "catalog_incomplete", "OmniRoute reported an incomplete model catalog."
                )
            rows = data.get("data")
            more = data.get("has_more", False)
            if not isinstance(rows, list) or not isinstance(more, bool):
                raise RoutingError(
                    "catalog_response", "OmniRoute returned an invalid model catalog."
                )
            yield rows
            if not more:
                return
            cursor = data.get("last_id")
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1000 or cursor in seen:
                break
            seen.add(cursor)
            params = {"limit": PAGE_SIZE, "after": cursor}
        raise RoutingError("catalog_paging", "OmniRoute model catalog pagination did not complete.")

    def decision_models(self) -> tuple[set[str], set[str]]:
        eligible = set()
        other = set()
        deadline = time.monotonic() + CATALOG_TIMEOUT_S
        for rows in self._catalog_pages("/systemone/models", deadline):
            for row in rows:
                if isinstance(row, dict) and _classifier_id(model := row.get("id")):
                    if row.get("api_format") == "systemone" and row.get("type") != "embedding":
                        eligible.add(model)
                    else:
                        other.add(model)
        return eligible, other

    def _verify_answer_combos(self, menu: Mapping[str, str], deadline: float) -> None:
        missing = set(menu.values())
        for rows in self._catalog_pages("/models", deadline):
            for row in rows:
                if (
                    isinstance(row, dict)
                    and row.get("owned_by") == "combo"
                    and row.get("type", "chat") == "chat"
                    and isinstance(model := row.get("id"), str)
                ):
                    missing.discard(model)
            if not missing:
                return
        raise RoutingError(
            "answer_combo_unavailable",
            "A configured answer combo is missing from this profile’s OmniRoute catalog.",
        )

    def classify(
        self, settings: RoutingSettings, decision_text: str, *, contextual=False, timeout_s=2.0
    ) -> str:
        if not _classifier_id(settings.classifier):
            raise RoutingError(
                "classifier_unset", "Select a provider-qualified OmniRoute System One model."
            )
        menu = settings.models
        unique_models = set(menu.values())
        if (
            set(menu) != set(TIERS)
            or len(unique_models) != len(TIERS)
            or settings.trigger in unique_models
            or not all(_model_id(model) for model in unique_models)
        ):
            raise RoutingError(
                "answer_combo_configuration",
                "Configure four distinct OmniRoute answer combos other than the trigger.",
            )
        if not decision_text or len(decision_text) > MAX_PROMPT_CHARS:
            raise RoutingError(
                "classifier_input", "No bounded input is available for turn routing."
            )
        deadline = time.monotonic() + timeout_s
        self._verify_answer_combos(menu, deadline)
        payload = {
            "model": settings.classifier,
            "state": decision_text,
            "questions": build_questions(menu, contextual=contextual),
        }
        result, headers = self._request("POST", "/systemone", deadline, payload=payload)
        if settings.classifier.startswith("ollama-local/"):
            _verify_local_model(settings.classifier, result, headers)
        try:
            answer = select_route(result, menu)["selected_model"]
        except (KeyError, TypeError, ValueError):
            raise RoutingError(
                "classifier_response", "The OmniRoute classifier returned an invalid choice."
            ) from None
        if answer not in unique_models:
            raise RoutingError(
                "classifier_choice",
                "The OmniRoute classifier did not choose an available answer combo.",
            )
        return answer


def _verify_local_model(classifier: str, result: dict, headers: Mapping[str, str]) -> None:
    # OmniRoute forwards Ollama's model field and reports the answered model
    # in this header, using the requested model only when upstream omits it.
    allowed = (classifier, classifier.removeprefix("ollama-local/"))
    reported = [result["model"]] if "model" in result else []
    if "X-OmniRoute-Model" in headers:
        reported.append(headers["X-OmniRoute-Model"])
    if not reported or any(model not in allowed for model in reported):
        raise RoutingError(
            "classifier_model_mismatch", "OmniRoute did not confirm the selected local classifier."
        )


def register_selection(ctx):
    usage = "Usage: /routing-classifier list or /routing-classifier select [provider/model]"

    def handle(raw_args: str) -> str:
        parts = raw_args.split()
        action = parts[0].lower() if parts else "list"
        if action not in ("list", "select") or len(parts) > (2 if action == "select" else 1):
            return usage
        model = parts[1] if len(parts) == 2 else None
        if model is not None and not _classifier_id(model):
            return "Use a provider-qualified OmniRoute model ID, such as provider/model."
        try:
            eligible, other = Gateway.from_profile().decision_models()
            ordered = sorted(eligible)
            if action == "list":
                selected = RoutingSettings.from_context(ctx).classifier or "unset"
                choices = "\n".join(ordered) or "No System One models are visible for this profile."
                return f"Selected classifier · {selected}\n{choices}\n\n{usage}"
            if model is None:
                if not eligible:
                    return f"No System One models are visible. Enter an explicit ID.\n{usage}"
                model = ctx.ask_choice("Select an OmniRoute System One classifier", ordered)
                if model is None:
                    return "\n".join(ordered) + f"\n\n{usage}"
            if model in other or (eligible and model not in eligible) or not _classifier_id(model):
                return "That model is absent from this profile’s System One catalog or uses another API format."
            ctx.set_config(CLASSIFIER_KEY, model)
            return f"Selected {model} for the next user turn. OmniRoute checks access on inference."
        except RoutingError as exc:
            return str(exc)
        except Exception:
            return "The OmniRoute classifier setting could not be accessed for this profile."

    ctx.register_command(
        "routing-classifier",
        handle,
        description="List or select this profile’s OmniRoute System One classifier",
        args_hint="list | select [provider/model]",
    )
