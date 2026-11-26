"""Profile-scoped, once-per-turn model routing at Hermes's main request boundary."""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from os import environ
from threading import BoundedSemaphore, Event, Lock, Thread, Timer
from weakref import ref

from .routing_gateway import Gateway, RoutingError, RoutingSettings, register_selection
from .routing_policy import MAX_PROMPT_CHARS, ROUTE_SPECS

logger = logging.getLogger(__name__)

DECISION_TIMEOUT = 2.0
STAGED_TTL = 300.0
TURN_TTL = 86400.0
MAX_TURNS = 256
MAX_DECISIONS = 4
MAX_HISTORY_ROWS = 96
SOURCE = "omniroute.turn-routing"

_ATTACHMENT_LINE = re.compile(
    r"^\s*\[(?:Image attached|Attached image|File attached|Attached file)[^\n]*", re.M | re.I
)
_DATA_URL = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[\w+/_=-]+", re.I)
_ACK_PHRASE = (
    r"yes|ok(?:ay)?|sure|thanks|thank you|great|sounds good|got it|done|of course|"
    r"no problem|will do|you(?:'re| are) welcome|(?:please )?(?:go ahead|proceed|continue|"
    r"do (?:it|that)|fix (?:it|that)|ship it|try again)(?: please)?|"
    r"(?:i|we) (?:will|can) (?:do|continue with) (?:that|it)"
)
_ACKNOWLEDGMENT = re.compile(rf"(?:{_ACK_PHRASE})(?:[,.!] +(?:{_ACK_PHRASE}))*[.!…]*", re.I)
_VISUAL_PARTS = frozenset({"image", "image_url", "input_image"})
_TEXT_PARTS = frozenset({"text", "input_text", "output_text"})
_CREDENTIAL_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "x-omniroute-api-key"})


def _has_visual(content):
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") in _VISUAL_PARTS for part in content
    )


def _text_and_visual(content, limit=MAX_PROMPT_CHARS):
    from agent.memory_context import fence_recalled_context
    from agent.redact import redact_sensitive_text
    from gateway.message_timestamps import strip_leading_message_timestamps
    from session_rules.turns import sanitize_context

    if isinstance(content, list):
        texts = (
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in _TEXT_PARTS
            and isinstance(part.get("text"), str)
        )
        visual = _has_visual(content)
    elif isinstance(content, str):
        texts, visual = (content,), False
    else:
        return "", False
    chunks = []
    remaining = limit * 2
    for text in texts:
        chunk = text[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk) + 1
        if remaining <= 0:
            break
    text = "\n".join(chunks)
    visual |= any(
        "image" in match.group().casefold().split(":", 1)[0]
        for match in _ATTACHMENT_LINE.finditer(text)
    )
    text = _ATTACHMENT_LINE.sub("", text)
    text = _DATA_URL.sub("[attachment omitted]", text)
    text = sanitize_context(fence_recalled_context(text))
    text, _ = strip_leading_message_timestamps(text)
    return redact_sensitive_text(text, force=True).strip()[:limit], visual


def _historical_user_input(message):
    from agent.conversation_compression_scaffolding import is_user_role_scaffolding
    from agent.message_metadata import typed_ask_text
    from agent.prompt_builder import STEER_MARKER_OPEN
    from gateway.message_timestamps import strip_leading_message_timestamps
    from gateway.response_filters import is_machinery_display_kind
    from session_rules.turns import user_originated_turn_view, withheld_prompt_text

    if isinstance(content := message.get("content"), str):
        clean, _ = strip_leading_message_timestamps(content)
        if clean != content:
            message = {**message, "content": clean}
    if (
        is_machinery_display_kind(message.get("display_kind"))
        or is_user_role_scaffolding(message)
        or withheld_prompt_text(message) is not None
    ):
        return "", False
    view = user_originated_turn_view(message)
    if view is None:
        return "", False
    content = view.get("content")
    typed = typed_ask_text(view)
    if typed is None and isinstance(content, list):
        # Older multimodal history may append plugin text in later parts without
        # a typed-ask stamp. The first caption is sufficient for task context.
        typed = next(
            (
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") in _TEXT_PARTS
                and isinstance(part.get("text"), str)
            ),
            "",
        )
    text, visual = _text_and_visual(typed if typed is not None else content, limit=1400)
    visual |= _has_visual(content)
    return ("", False) if text.startswith(STEER_MARKER_OPEN) else (text, visual)


def render_decision_input(user_message, conversation_history, *, prior_tier=None, has_visual=False):
    """Build a bounded decision snapshot from the original request and preceding transcript."""
    from session_rules.turns import is_compaction_summary_message

    current, visual = _text_and_visual(user_message)
    visual |= has_visual
    if not (current or visual):
        return "", False

    prior_user = prior_assistant = current_assistant = ""
    prior_visual = False
    for message in reversed(conversation_history[-MAX_HISTORY_ROWS:]):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            text, image = _historical_user_input(message)
            if text or image:
                prior_assistant = prior_assistant or current_assistant
                if image or not _ACKNOWLEDGMENT.fullmatch(text):
                    prior_user, prior_visual = text, image
                    break
            current_assistant = ""
        elif (
            message.get("role") == "assistant"
            and not prior_assistant
            and not current_assistant
            and not is_compaction_summary_message(message)
        ):
            text, _ = _text_and_visual(message.get("content"), limit=2000)
            if text and not _ACKNOWLEDGMENT.fullmatch(text):
                current_assistant = text

    if not (visual or prior_visual or prior_user or prior_assistant or prior_tier in ROUTE_SPECS):
        return current, False

    annotations = []
    if visual or prior_visual:
        where = "current request" if visual else "preceding task"
        annotations.append(f"\n\nVisual input is present in the {where}.")
    if prior_tier in ROUTE_SPECS:
        annotations.append(f"\n\nPrevious tier: {prior_tier}")
    prefix = "Current request:\n"
    notes = "".join(annotations)
    rendered = prefix + current[: MAX_PROMPT_CHARS - len(prefix + notes)] + notes
    user_title = "\n\nPreceding user task:\n"
    assistant_title = "\n\nPreceding assistant plan or result:\n"
    remaining = MAX_PROMPT_CHARS - len(rendered)
    if prior_user:
        user_budget = remaining - len(user_title)
        if prior_assistant and user_budget > len(assistant_title) + 1:
            user_budget = (user_budget - len(assistant_title)) // 2
        if user_budget > 0:
            rendered += user_title + prior_user[:user_budget]
    assistant_budget = MAX_PROMPT_CHARS - len(rendered) - len(assistant_title)
    if prior_assistant and assistant_budget > 0:
        rendered += assistant_title + prior_assistant[:assistant_budget]
    return rendered, True


@dataclass
class _Turn:
    settings: RoutingSettings
    sender_id: str
    session_id: str
    expires: float
    text: str
    contextual: bool
    ready: Event = field(default_factory=Event)
    deadline: float = 0.0
    model: str | None = None
    base_url: str | None = None
    identity: tuple[str, bytes] | None = None
    identity_change_logged: bool = False


class _Router:
    def __init__(self, ctx, home):
        self.ctx = ctx
        self.home = home
        self.lock = Lock()
        self.turns = {}
        self.prior_tiers = OrderedDict()
        self.workers = BoundedSemaphore(MAX_DECISIONS)
        self.expiration = None

    def _prune(self, now):
        for key in [key for key, turn in self.turns.items() if turn.expires <= now]:
            del self.turns[key]

    def _schedule_expiration(self, now):
        if self.expiration is not None:
            return
        expires = min((turn.expires for turn in self.turns.values() if turn.text), default=None)
        if expires is not None:
            router_ref = ref(self)

            def expire():
                if router := router_ref():
                    router._expire_staged()

            self.expiration = Timer(max(0.0, expires - now), expire)
            self.expiration.daemon = True
            self.expiration.start()

    def _expire_staged(self):
        with self.lock:
            self.expiration = None
            now = time.monotonic()
            self._prune(now)
            self._schedule_expiration(now)

    def stage(
        self,
        *,
        session_id,
        task_id,
        turn_id,
        user_message,
        conversation_history,
        model,
        platform,
        parent_session_id="",
        sender_id="",
    ):
        from gateway.response_filters import is_machinery_display_kind
        from hermes_constants import hermes_home_key

        if (
            hermes_home_key() != self.home
            or not turn_id
            or not task_id
            or not session_id
            or parent_session_id
            or platform in ("", "cron", "subagent", "webhook")
        ):
            return
        settings = RoutingSettings.from_context(self.ctx)
        if not settings.enabled or model != settings.trigger:
            return
        # Hermes v0.23.4 always appends this turn's raw/API-facing row before invoking this hook.
        # Use the separate original request and read only attachment signals from the current row.
        history = conversation_history or []
        current = history[-1] if history and isinstance(history[-1], dict) else {}
        if current.get("role") != "user" or is_machinery_display_kind(current.get("display_kind")):
            return
        has_visual = _has_visual(current.get("content"))
        key = (task_id, turn_id)
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            if key in self.turns or len(self.turns) >= MAX_TURNS:
                return
            prior_tier = self.prior_tiers.get((session_id, sender_id))
        text, contextual = render_decision_input(
            user_message, history[:-1], prior_tier=prior_tier, has_visual=has_visual
        )
        if not text:
            return
        with self.lock:
            if key not in self.turns and len(self.turns) < MAX_TURNS:
                now = time.monotonic()
                self.turns[key] = _Turn(
                    settings, sender_id, session_id, now + STAGED_TTL, text, contextual
                )
                self._schedule_expiration(now)

    def finish(self, *, task_id="", turn_id=""):
        from hermes_constants import hermes_home_key

        if task_id and turn_id and hermes_home_key() == self.home:
            with self.lock:
                turn = self.turns.pop((task_id, turn_id), None)
                if turn is not None:
                    turn.text = ""
                    turn.ready.set()

    def rewrite(
        self,
        *,
        request,
        original_request,
        task_id,
        turn_id,
        session_id,
        provider,
        base_url,
        api_mode,
        model,
    ):
        from hermes_constants import hermes_home_key

        if (
            provider != "omniroute"
            or api_mode != "chat_completions"
            or hermes_home_key() != self.home
            or not session_id
        ):
            return
        if not all(
            payload.get("model") == model
            and "model" not in (payload.get("extra_body") or {})
            and not _CREDENTIAL_HEADERS.intersection(
                name.lower() for name in (payload.get("extra_headers") or {})
            )
            for payload in (request, original_request)
        ):
            return

        key = (task_id, turn_id)
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            turn = self.turns.get(key)
            if turn is None or model != turn.settings.trigger:
                return
            turn.expires = now + TURN_TTL
            start = not turn.deadline
            if start:
                turn.deadline = now + DECISION_TIMEOUT
                turn.base_url = base_url
                text, turn.text = turn.text, ""
            elif turn.base_url != base_url:
                return

        if start:
            self._start_decision(turn, text, base_url)
        turn.ready.wait(max(0.0, turn.deadline - time.monotonic()))

        with self.lock:
            if self.turns.get(key) is not turn:
                return
            if not turn.ready.is_set():
                self._resolve(turn, reason="classifier timeout")
            model_choice, identity = turn.model, turn.identity
        if model_choice is None:
            return
        try:
            same_identity = Gateway.active_identity(expected_base_url=base_url) == identity
        except Exception:
            same_identity = False
        with self.lock:
            if self.turns.get(key) is not turn:
                return
            if not same_identity:
                if not turn.identity_change_logged:
                    turn.identity_change_logged = True
                    logger.warning(
                        "OmniRoute turn routing passed through after chat identity changed"
                    )
                return
            tier = next(
                tier for tier, combo in turn.settings.models.items() if combo == model_choice
            )
            for sid in (turn.session_id, session_id):
                prior_key = (sid, turn.sender_id)
                self.prior_tiers[prior_key] = tier
                self.prior_tiers.move_to_end(prior_key)
            while len(self.prior_tiers) > MAX_TURNS:
                self.prior_tiers.popitem(last=False)
        return {
            "request": {**request, "model": model_choice},
            "source": SOURCE,
            "reason": "selected for user turn",
        }

    def _start_decision(self, turn, text, base_url):
        try:
            # Hermes's active request credentials exist only on this thread's live turn context.
            gateway = Gateway.from_profile(expected_base_url=base_url)
        except RoutingError as exc:
            with self.lock:
                self._resolve(turn, reason=exc.reason)
            return
        except Exception:
            with self.lock:
                self._resolve(turn, reason="gateway identity unavailable")
            return
        with self.lock:
            if turn.ready.is_set():
                return
            turn.identity = gateway.identity
        if not self.workers.acquire(blocking=False):
            with self.lock:
                self._resolve(turn, reason="classifier busy")
            return

        def classify():
            try:
                if turn.ready.is_set():
                    return
                timeout = turn.deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError
                choice = gateway.classify(
                    turn.settings, text, contextual=turn.contextual, timeout_s=timeout
                )
                if choice == turn.settings.trigger or choice not in turn.settings.models.values():
                    raise RoutingError(
                        "invalid_answer_route", "Classifier returned an unavailable answer route."
                    )
                with self.lock:
                    self._resolve(turn, model=choice)
            except RoutingError as exc:
                with self.lock:
                    self._resolve(turn, reason=exc.reason)
            except TimeoutError:
                with self.lock:
                    self._resolve(turn, reason="classifier timeout")
            except Exception:
                with self.lock:
                    self._resolve(turn, reason="classifier unavailable")
            finally:
                self.workers.release()

        try:
            Thread(target=classify, name="omniroute-turn-decision", daemon=True).start()
        except RuntimeError:
            self.workers.release()
            with self.lock:
                self._resolve(turn, reason="classifier unavailable")

    @staticmethod
    def _resolve(turn, *, model=None, reason=None):
        if turn.ready.is_set():
            return
        if time.monotonic() >= turn.deadline:
            model, reason = None, "classifier timeout"
        turn.model = model
        if reason:
            logger.warning(
                "OmniRoute turn routing passed through to the selected combo (%s)", reason
            )
        turn.ready.set()


def register(ctx):
    if not all(
        callable(getattr(ctx, name, None))
        for name in (
            "get_config",
            "set_config",
            "register_command",
        )
    ):
        return

    register_selection(ctx)
    if not RoutingSettings.from_context(ctx).enabled:
        return
    if environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        logger.warning("OmniRoute turn routing requires access to the in-process chat credential")
        return
    if not all(
        callable(getattr(ctx, name, None)) for name in ("register_hook", "register_middleware")
    ):
        return
    try:
        from hermes_constants import hermes_home_key
    except ImportError:
        logger.warning("OmniRoute turn routing needs Hermes profile-scoped middleware support")
        return

    router = _Router(ctx, hermes_home_key())
    ctx.register_hook("pre_llm_call", router.stage)
    ctx.register_middleware("llm_request", router.rewrite)
    # Hermes fires this for vetoed, failed and interrupted turns as well as normal replies.
    ctx.register_hook("on_session_end", router.finish)
