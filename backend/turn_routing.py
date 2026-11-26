"""Profile-scoped model routing at Hermes's main request boundary."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from os import environ
from threading import BoundedSemaphore, Event, Lock, Thread, Timer
from weakref import ref

from .routing_gateway import Gateway, RoutingError, RoutingSettings, register_selection
from .routing_policy import MAX_PROMPT_CHARS, ROUTE_SPECS
from .routing_progress import tool_progress

logger = logging.getLogger(__name__)

DECISION_TIMEOUT = 2.0
STAGED_TTL = 300.0
TURN_TTL = 86400.0
MAX_TURNS = 256
MAX_DECISIONS = 4
MAX_PROGRESS_BATCHES = 256
MAX_HISTORY_EXCHANGES = 96
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


_CONTEXT_REFERENCE = re.compile(
    r"\b(it|this|that|these|those|them|one|ones|next|else|ready|news|updates?|again|"
    r"proceed|instead|both|same|first|second|option|still|progress|findings?|status)\b"
    r"|^(?:what|how|where) (?:did|do|have|are) (?:you|we)\b",
    re.I,
)


def _refers_back(text):
    if re.search(r"\b(?:new|separate|unrelated) (?:task|question|topic)\b", text, re.I):
        return False
    return len(text.split()) <= 12 and bool(_CONTEXT_REFERENCE.search(text))


def _previous_work(history):
    """Find the work a short continuation refers to, using only user and assistant text."""
    from session_rules.turns import is_compaction_summary_message

    newest_user = newest_reply = reply = ""
    latest_update = ""
    exchanges = 0
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            if reply or message.get("tool_calls") or is_compaction_summary_message(message):
                continue
            text, _ = _text_and_visual(message.get("content"), limit=2000)
            if text and not _ACKNOWLEDGMENT.fullmatch(text):
                reply = text
            continue
        if message.get("role") != "user":
            continue
        text, visual = _historical_user_input(message)
        if not (text or visual):
            continue
        acknowledgment = _ACKNOWLEDGMENT.fullmatch(text)
        if visual or (not acknowledgment and not _refers_back(text)):
            return text, reply, visual, latest_update
        if not acknowledgment and not newest_user:
            newest_user, newest_reply = text, reply
        # A thank-you followed by a courtesy reply carries no new task state.
        social = re.match(r"(?:thanks|thank you|great|got it|sounds good|of course)\b", text, re.I)
        if not latest_update and not social:
            parts = []
            if not acknowledgment and (not text.endswith("?") or len(text.split()) >= 6):
                parts.append(text)
            if len(reply.split()) >= 8:
                parts.append(reply)
            latest_update = "\n".join(parts)
        exchanges += not bool(acknowledgment)
        if exchanges >= MAX_HISTORY_EXCHANGES:
            break
        reply = ""
    return newest_user, newest_reply or latest_update, False, ""


def _compacted_user_task(history):
    """Read only Hermes's deterministic quoted user task from a marked full compaction."""
    from ast import literal_eval

    from session_rules.turns import (
        COMPRESSED_SUMMARY_METADATA_KEY,
        HISTORICAL_TASK_HEADING,
        MICRO_COMPACT_MARKER_KEY,
        SUMMARY_PREFIX,
    )

    field = "User asked (deterministic, from compacted turns): "
    for row in reversed(history):
        if not isinstance(row, dict) or row.get(COMPRESSED_SUMMARY_METADATA_KEY) is not True:
            continue
        if row.get("role") != "user" or row.get(MICRO_COMPACT_MARKER_KEY):
            return ""
        content = row.get("content")
        if not isinstance(content, str) or not content.startswith(SUMMARY_PREFIX):
            return ""
        # Only the native first section qualifies. Other sections can contain
        # tool output, older summaries or quoted text that looks like a task.
        first = content[len(SUMMARY_PREFIX) : len(SUMMARY_PREFIX) + 2048].lstrip().splitlines()
        if len(first) < 3 or first[0] != HISTORICAL_TASK_HEADING or not first[1].startswith(field):
            return ""
        if first[2] != "Historical only; newer protected-tail messages after this summary win.":
            return ""
        quoted = first[1][len(field) :]
        if not quoted.startswith(("'", '"')):
            return ""
        try:
            task = literal_eval(quoted)
        except (SyntaxError, ValueError):
            return ""
        if not isinstance(task, str) or repr(task) != quoted:
            return ""
        task, _ = _text_and_visual(task, limit=1400)
        return "" if _ACKNOWLEDGMENT.fullmatch(task) or _refers_back(task) else task
    return ""


def render_decision_input(user_message, conversation_history, *, prior_tier=None, has_visual=False):
    """Build a bounded decision snapshot from the original request and preceding transcript."""
    current, visual = _text_and_visual(user_message)
    visual |= has_visual
    if not (current or visual):
        return "", False

    previous, reply, previous_visual, progress = _previous_work(conversation_history)
    if (
        not visual
        and not previous_visual
        and (not previous or _refers_back(previous))
        and (_ACKNOWLEDGMENT.fullmatch(current) or _refers_back(current))
    ):
        previous = _compacted_user_task(conversation_history) or previous
    if not (visual or previous_visual or previous or reply or prior_tier in ROUTE_SPECS):
        return current, False

    notes = []
    if visual or previous_visual:
        where = "current request" if visual else "preceding task"
        notes.append(f"Visual input is present in the {where}.")
    if prior_tier in ROUTE_SPECS:
        notes.append(f"Previous tier: {prior_tier}")

    if previous and _ACKNOWLEDGMENT.fullmatch(current):
        # On an approval, lead with what the user authorized. Repeating the approval
        # as the main task makes a small classifier label substantive work as routine.
        suffix = "\n\n".join([f"Latest user instruction: {current}", *notes])
        parts = [previous, reply]
        if progress:
            parts.append("Latest progress: " + progress)
        state = "\n\n".join(part for part in parts if part)
        return state[: MAX_PROMPT_CHARS - len(suffix) - 2] + "\n\n" + suffix, True

    prefix = "Current request:\n"
    notes = "".join("\n\n" + note for note in notes)
    rendered = prefix + current[: MAX_PROMPT_CHARS - len(prefix + notes)] + notes
    sections = [
        ("Preceding user task", previous),
        ("Preceding assistant plan or result", reply),
        ("Latest progress", progress),
    ]
    sections = [(f"\n\n{title}:\n", text) for title, text in sections if text]
    remaining = MAX_PROMPT_CHARS - len(rendered) - sum(len(label) for label, _ in sections)
    for index, (label, text) in enumerate(sections):
        limit = remaining // (len(sections) - index)
        if limit < 1:
            break
        rendered += label + text[:limit]
        remaining -= min(limit, len(text))
    return rendered, True


def render_progress_input(task, summary):
    """Combine the staged request and safe tool signals within the classifier limit."""
    prefix = "Original turn context:\n"
    progress = (
        f"\n\nCompleted tool progress:\n{summary[:1200]}"
        "\n\nAssess what still needs to be done, including the answer the user asked for."
    )
    task_budget = MAX_PROMPT_CHARS - len(prefix + progress)
    if len(task) > task_budget:
        tail = task_budget // 4
        task = task[: task_budget - tail - 3] + " … " + task[-tail:]
    return prefix + task + progress


@dataclass
class _Decision:
    deadline: float
    model: str | None = None
    ready: Event = field(default_factory=Event)


@dataclass
class _Turn:
    settings: RoutingSettings
    sender_id: str
    session_id: str
    expires: float
    text: str = field(repr=False)
    contextual: bool
    base_url: str | None = None
    identity: tuple[str, bytes] | None = None
    identity_change_logged: bool = False
    current: _Decision | None = None
    batches: dict[bytes, _Decision] = field(default_factory=dict)

    def close(self):
        self.text = ""
        self.batches.clear()
        if self.current is not None:
            self.current.ready.set()

    def tier(self, model):
        return next((tier for tier, combo in self.settings.models.items() if combo == model), None)


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
            self.turns.pop(key).close()

    def _schedule_expiration(self, now):
        expires = min((turn.expires for turn in self.turns.values()), default=None)
        if self.expiration is not None:
            armed_at, armed_timer = self.expiration
            if expires is not None and armed_at <= expires:
                return
            self.expiration = None
            armed_timer.cancel()
        if expires is None:
            return
        router_ref = ref(self)

        def expire():
            if router := router_ref():
                router._expire_turns(timer)

        timer = Timer(max(0.0, expires - now), expire)
        self.expiration = expires, timer
        timer.daemon = True
        timer.start()

    def _expire_turns(self, timer):
        with self.lock:
            if self.expiration is None or self.expiration[1] is not timer:
                return
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
                    turn.close()
                    self._schedule_expiration(time.monotonic())

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
            or not base_url
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
            decision = turn.current
            start = decision is None
            if start:
                decision = turn.current = _Decision(now + DECISION_TIMEOUT)
                turn.expires = now + TURN_TTL
                turn.base_url = base_url
                text = turn.text
            elif turn.base_url != base_url:
                return

        if start:
            self._start_decision(turn, decision, text, contextual=turn.contextual)
        else:
            progress = tool_progress(original_request.get("messages"))
            if progress is not None:
                if turn.identity is None:
                    self._wait_for(decision)
                if turn.identity is None or not self._matches_identity(turn):
                    return
                decision = self._progress_decision(turn, progress)

        self._wait_for(decision)
        model_choice = decision.model
        if model_choice is None or not self._matches_identity(turn):
            return
        with self.lock:
            if self.turns.get(key) is not turn:
                return
            if decision is turn.current:
                tier = turn.tier(model_choice)
                for sid in (turn.session_id, session_id):
                    prior_key = (sid, turn.sender_id)
                    self.prior_tiers[prior_key] = tier
                    self.prior_tiers.move_to_end(prior_key)
                while len(self.prior_tiers) > MAX_TURNS:
                    self.prior_tiers.popitem(last=False)
        return {
            "request": {**request, "model": model_choice},
            "source": SOURCE,
            "reason": "selected for current turn step",
        }

    def _matches_identity(self, turn):
        try:
            if Gateway.active_identity(expected_base_url=turn.base_url) == turn.identity:
                return True
        except Exception:
            pass
        with self.lock:
            if not turn.identity_change_logged:
                turn.identity_change_logged = True
                logger.warning("OmniRoute turn routing passed through after chat identity changed")
        return False

    def _progress_decision(self, turn, progress):
        call_ids, summary = progress
        # Store only a fixed-size fingerprint, so opaque call IDs stay out of routing state.
        batch = sha256(json.dumps(call_ids).encode()).digest()
        deadline = time.monotonic() + DECISION_TIMEOUT
        while True:
            with self.lock:
                current = turn.current
                if previous := turn.batches.get(batch):
                    return previous
                if not turn.text or len(turn.batches) >= MAX_PROGRESS_BATCHES:
                    return current
                if current.ready.is_set():
                    decision = _Decision(deadline, current.model)
                    turn.current = turn.batches[batch] = decision
                    text = render_progress_input(turn.text, summary)
                    break
            self._wait_for(current)
        self._start_decision(turn, decision, text, contextual=True)
        return decision

    def _wait_for(self, decision):
        if not decision.ready.wait(max(0.0, decision.deadline - time.monotonic())):
            with self.lock:
                self._resolve(decision, reason="classifier timeout")

    def _start_decision(self, turn, decision, text, *, contextual):
        if decision.ready.is_set() or time.monotonic() >= decision.deadline:
            return
        try:
            # Hermes's active request credentials exist only on this thread's live turn context.
            gateway = Gateway.from_profile(expected_base_url=turn.base_url)
            identity = gateway.identity
        except Exception as exc:
            reason = exc.reason if isinstance(exc, RoutingError) else "gateway identity unavailable"
            with self.lock:
                if turn.identity is None:
                    turn.text = ""
                self._resolve(decision, reason=reason)
            return
        with self.lock:
            if decision.ready.is_set():
                return
            if turn.identity is None:
                turn.identity = identity
            elif turn.identity != identity:
                self._resolve(decision, reason="chat identity changed")
                return
        if not self.workers.acquire(blocking=False):
            with self.lock:
                self._resolve(decision, reason="classifier busy")
            return

        def classify():
            try:
                if decision.ready.is_set():
                    return
                timeout = decision.deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError
                choice = gateway.classify(
                    turn.settings, text, contextual=contextual, timeout_s=timeout
                )
                if choice == turn.settings.trigger or choice not in turn.settings.models.values():
                    raise RoutingError(
                        "invalid_answer_route", "Classifier returned an unavailable answer route."
                    )
                with self.lock:
                    self._resolve(decision, model=choice)
            except RoutingError as exc:
                with self.lock:
                    self._resolve(decision, reason=exc.reason)
            except TimeoutError:
                with self.lock:
                    self._resolve(decision, reason="classifier timeout")
            except Exception:
                with self.lock:
                    self._resolve(decision, reason="classifier unavailable")
            finally:
                self.workers.release()

        try:
            Thread(target=classify, name="omniroute-turn-decision", daemon=True).start()
        except RuntimeError:
            self.workers.release()
            with self.lock:
                self._resolve(decision, reason="classifier unavailable")

    @staticmethod
    def _resolve(decision, *, model=None, reason=None):
        if decision.ready.is_set():
            return
        if time.monotonic() >= decision.deadline:
            model, reason = None, "classifier timeout"
        if model is not None:
            decision.model = model
        elif decision.model is not None:
            logger.warning("OmniRoute turn routing kept the previous choice (%s)", reason)
        else:
            logger.warning(
                "OmniRoute turn routing passed through to the selected combo (%s)", reason
            )
        decision.ready.set()


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
