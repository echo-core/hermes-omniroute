"""Profile-scoped model routing at Hermes's main request boundary."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import islice
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
_RESUME = re.compile(
    r"(?:go(?: ahead)?|proceed|continue|do (?:it|that)|fix (?:it|that)|ship it|try again|yes)\b",
    re.I,
)
_RECEIPT = re.compile(
    r"(?:understood|noted|much appreciated|gotcha|got it|thanks|thank you|okay|ok)"
    r"(?:[, ]+(?:thanks|thank you))?[.!…]*",
    re.I,
)
_IDLE_REPLY = re.compile(
    r"(?:(?:okay|ok|understood|sure|of course|no problem)[,.!]?\s+)?"
    r"(?:i(?:['’]ll| will) (?:wait|hold off)(?: for (?:now|your (?:go-ahead|approval|confirmation)))?"
    r"|you(?:['’]re| are) welcome|standing by|of course|no problem)[.!…]*",
    re.I,
)
_PENDING_OFFER = re.compile(
    r"\b(?:(?:shall|should) (?:i|we)|(?:would you like|do you want)(?: me| us)? to)\b"
    r"|\b(?:awaiting|pending|once (?:i|we) (?:have|get)) your (?:approval|confirmation)\b",
    re.I,
)
_CLOSED_WORK = re.compile(
    r"\b(?:is|was|already|have|has|been) (?:already )?"
    r"(?:complete(?:d)?|done|finished|delivered|cancel[l]?ed|withdrawn|resolved)\b"
    r"|\b(?:no (?:pending|remaining) work|nothing (?:left|pending))\b",
    re.I,
)
_VISUAL_PARTS = frozenset({"image", "image_url", "input_image"})
_TEXT_PARTS = frozenset({"text", "input_text", "output_text"})
_CREDENTIAL_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "x-omniroute-api-key"})
_REFERENCE_REQUEST_LIMIT = 320
_REFERENCE_ITEM_LIMIT = 1400
_INLINE_ITEM_LIMIT = 240
_ORDINALS = {
    word: number
    for number, word in enumerate(
        "first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1
    )
}
_CARDINALS = {
    word: number
    for number, word in enumerate("one two three four five six seven eight nine ten".split(), 1)
}
_ORDINAL = rf"(?ai:{'|'.join(_ORDINALS)}|[1-9][0-9]?(?:st|nd|rd|th))"
_CARDINAL = rf"(?ai:{'|'.join(_CARDINALS)}|[1-9][0-9]?)"
_ITEM_NOUN = (
    r"(?ai:action|choice|entry|example|finding|issue|item|option|point|proposal|"
    r"reason|recommendation|result|step|suggestion|task)"
)
_NUMBERED_REFERENCE = re.compile(
    rf"\b(?:(?P<ordinal>{_ORDINAL})\s+(?:{_ITEM_NOUN}|one)"
    rf"|{_ITEM_NOUN}\s+(?:(?:number|no\.?)\s+|#\s*)?(?P<number>{_CARDINAL}))\b",
    re.I,
)
_OTHER_NUMBER = re.compile(
    rf"\b{_ORDINAL}\b|#\s*[0-9]|\b(?:both|each)\b"
    r"|\b(?:and|or|through|to|versus|vs\.?)\s+"
    rf"(?:(?:the\s+)?(?:rest|other|remaining|last|former|latter)\b|{_CARDINAL}\b)",
    re.I,
)
_TOPIC_CHANGE = re.compile(
    r"\b(?:new|separate|unrelated|different|another) (?:task|question|topic|subject)\b"
    r"|\b(?:change|switch) (?:the )?(?:subject|topics?)\b|\bon a different note\b"
    r"|[,.;?!]\s+(?:and\s+)?(?:also|separately)\b",
    re.I,
)
_LIST_ITEM = re.compile(r"^([ \t]*)(\*\*)?([0-9]+)([.)])(?:\*\*)?(?:[ \t]+|$)", re.M)
_LIST_BOUNDARY = re.compile(
    r"(?:#{1,6}[ \t]+|[-+*>][ \t]+|(?:[-*_][ \t]*){3,}$|\*\*.+\*\*:?[ \t]*$)"
)
_UNSAFE_ITEM_MARKUP = re.compile(r"^[ \t]*(?:`{3,}|~{3,})|<\s*(?:pre|code|img)\b|!\[", re.M | re.I)
_ITEM_SUBJECT = re.compile(r"\s+(?:of|in|for|to|from|on|about)\s+", re.I)
_REFERENCE_QUALIFIER = re.compile(
    r"(?:(?:the|your|our) (?:above|earlier|last|previous|prior)|"
    r"your (?:answer|list|review|response|plan|suggestions|recommendations|findings|examples)|"
    r"(?:one|a single|a|two|three) (?:short |simple |plain )?(?:sentences?|paragraphs?|bullets?)|"
    r"(?:plain|simple) (?:english|language|text))\b",
    re.I,
)
_REFERENCE_DETERMINER = re.compile(r"\b(?:the|your|our|that|this)\s+$", re.I)
_INLINE_ITEM_MARKUP = re.compile(r"[`*_~#\[\]<>|'\"‘’“”]|\w+://|\bwww\.", re.I)
_DELEGATION_PREFIX = re.compile(
    r"(?:please\s+)?(?:do|use|choose|pick|take|follow|implement|pursue|try|apply|"
    r"(?:go(?: ahead)?|proceed) with)\s+",
    re.I,
)


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


def _numbered_reference(text):
    if len(text) > _REFERENCE_REQUEST_LIMIT or any(char in text for char in "\r\n;"):
        return None
    if _TOPIC_CHANGE.search(text) or re.search(r"`|[\[\]<>]|https?://", text):
        return None
    matches = list(_NUMBERED_REFERENCE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    before, after = text[: match.start()], text[match.end() :]
    if _OTHER_NUMBER.search(before) or _OTHER_NUMBER.search(after):
        return None
    # An ordinal or number that introduces its own subject may belong to a new task.
    if (preposition := _ITEM_SUBJECT.match(after)) and not _REFERENCE_QUALIFIER.match(
        after, preposition.end()
    ):
        return None
    if re.match(r"[./]\s*[0-9]|\s*[,/&—–-]\s*(?:#\s*)?[0-9]", after):
        return None
    if cardinal := match["number"]:
        number = int(cardinal) if cardinal.isdigit() else _CARDINALS[cardinal.casefold()]
    else:
        ordinal = match["ordinal"].casefold()
        number = _ORDINALS.get(ordinal)
        if number is None:
            number = int(ordinal[:-2])
            suffix = "th"
            if not 11 <= number <= 13:
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
            if not ordinal.endswith(suffix):
                return None
    return number, match


def _only_delegates(current):
    """Whether the instruction adds only consent to carry out a selected item."""
    reference = _numbered_reference(current)
    if reference is None:
        return False
    _, match = reference
    prefix = _REFERENCE_DETERMINER.sub("", current[: match.start()])
    return bool(
        _DELEGATION_PREFIX.fullmatch(prefix)
        and re.fullmatch(r"(?:\s+please)?[.!…]*", current[match.end() :], re.I)
    )


def _latest_reference_reply(history):
    """Use only the newest substantive answer before a real user or compaction boundary."""
    from gateway.response_filters import is_machinery_display_kind
    from session_rules.turns import is_compaction_summary_message

    for message in islice(reversed(history), MAX_HISTORY_EXCHANGES):
        if not isinstance(message, dict):
            continue
        if is_compaction_summary_message(message):
            break
        role = message.get("role")
        if role == "user":
            if any(_historical_user_input(message)):
                break
            continue
        if (
            role != "assistant"
            or message.get("tool_calls")
            or is_machinery_display_kind(message.get("display_kind"))
        ):
            continue
        content = message.get("content")
        if isinstance(content, list):
            # Content blocks can mix attachments and text. Keep the selective path
            # to plain answers so a visible attachment cannot lose its routing hint.
            break
        if not isinstance(content, str):
            continue
        if len(content) > MAX_PROMPT_CHARS:
            break
        text, visual = _text_and_visual(content)
        if visual:
            break
        if text and not _ACKNOWLEDGMENT.fullmatch(text):
            return text
    return ""


def _numbered_item(reply, number):
    """Extract one item from a single consistently numbered, top-level list."""
    if _UNSAFE_ITEM_MARKUP.search(reply):
        return ""
    markers = list(_LIST_ITEM.finditer(reply))
    if not 1 <= number <= len(markers):
        return ""
    indent, bold, _, separator = markers[0].groups()
    if len(indent) > 3 or "\t" in indent:
        return ""

    selected = ""
    for position, marker in enumerate(markers, 1):
        if marker.groups() != (indent, bold, str(position), separator):
            return ""
        next_item = position < len(markers)
        end = markers[position].start() if next_item else len(reply)
        lines = reply[marker.start() : end].splitlines()
        if not lines[0][marker.end() - marker.start() :].strip():
            return ""
        content_indent = marker.end() - marker.start() if not bold else len(indent)
        blank = False
        for index, line in enumerate(lines[1:], 1):
            clean = line.lstrip()
            if not clean:
                blank = True
                continue
            leading = len(line) - len(clean)
            if blank and (leading >= content_indent + 4 or line.startswith("\t")):
                return ""
            unindented = leading <= len(indent)
            if (unindented and _LIST_BOUNDARY.match(clean)) or (blank and leading < content_indent):
                if next_item:
                    return ""
                lines = lines[:index]
                break
            blank = False
        if position == number:
            selected = "\n".join(lines).strip()
    return selected if len(selected) <= _REFERENCE_ITEM_LIMIT else ""


def _render_reference(current, match, item):
    """Inline short plain text; keep formatted findings intact in their own section."""
    marker = _LIST_ITEM.match(item)
    body = item[marker.end() :].removesuffix(".").rstrip()
    if (
        0 < len(body) <= _INLINE_ITEM_LIMIT
        and "\n" not in item
        and not marker.group(2)
        and not _INLINE_ITEM_MARKUP.search(body)
    ):
        determiner = _REFERENCE_DETERMINER.search(current[: match.start()])
        start = determiner.start() if determiner else match.start()
        return f"{current[:start]}“{body}”{current[match.end() :]}"
    return f"Current request:\n{current}\n\nPreceding assistant plan or result:\n{item}"


def _pending_work_before_receipts(history):
    """Recover an offered task across adjacent receipts when a later reply accepts the offer."""
    from gateway.response_filters import is_machinery_display_kind
    from session_rules.turns import is_compaction_summary_message

    rows = iter(islice(reversed(history), MAX_HISTORY_EXCHANGES))
    skipped = False
    for answer in rows:
        ask = next(rows, None)
        if not (
            isinstance(answer, dict)
            and answer.get("role") == "assistant"
            and isinstance(content := answer.get("content"), str)
            and not answer.get("tool_calls")
            and not is_machinery_display_kind(answer.get("display_kind"))
            and isinstance(ask, dict)
            and ask.get("role") == "user"
            and not is_compaction_summary_message(ask)
        ):
            break
        task, visual = _historical_user_input(ask)
        if not (task or visual):
            break
        if not visual and _RECEIPT.fullmatch(task):
            if len(content) > 240 or not (
                _IDLE_REPLY.fullmatch(content.strip()) or _RECEIPT.fullmatch(content.strip())
            ):
                break
            skipped = True
            continue
        if skipped and not _ACKNOWLEDGMENT.fullmatch(task) and not _refers_back(task):
            plan, attachment = _text_and_visual(content, limit=2000)
            # A quoted or superseded offer earlier in the answer is insufficient.
            closing = re.split(r"[.!?]\s+|\n+", plan)[-1]
            if (
                not attachment
                and len(content) <= 2000
                and _PENDING_OFFER.search(closing)
                and not _CLOSED_WORK.search(plan)
            ):
                return task, plan, visual, ""
        break
    return None


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


def render_turn_context(user_message, conversation_history, *, prior_tier=None, has_visual=False):
    """Return the decision snapshot, context flag, user objective and reference flag."""
    current, visual = _text_and_visual(user_message)
    visual |= has_visual
    if not (current or visual):
        return "", False, "", False

    pending = None
    bare_go = current.rstrip(".!…").casefold() == "go"
    if not visual and (bare_go or (_ACKNOWLEDGMENT.fullmatch(current) and _RESUME.search(current))):
        pending = _pending_work_before_receipts(conversation_history)
    previous, reply, previous_visual, progress = pending or _previous_work(conversation_history)
    if (
        not visual
        and not previous_visual
        and (not previous or _refers_back(previous))
        and (_ACKNOWLEDGMENT.fullmatch(current) or _refers_back(current))
    ):
        previous = _compacted_user_task(conversation_history) or previous
    if not (visual or previous_visual or previous or reply or prior_tier in ROUTE_SPECS):
        return current, False, current, False

    if not (visual or previous_visual) and (reference := _numbered_reference(current)) is not None:
        number, match = reference
        item = _numbered_item(_latest_reference_reply(conversation_history), number)
        if item:
            return _render_reference(current, match, item), True, current, True

    notes = []
    if visual or previous_visual:
        where = "current request" if visual else "preceding task"
        notes.append(f"Visual input is present in the {where}.")
    if prior_tier in ROUTE_SPECS:
        notes.append(f"Previous tier: {prior_tier}")

    if previous and (pending or _ACKNOWLEDGMENT.fullmatch(current)):
        # On an approval, lead with what the user authorized. Repeating the approval
        # as the main task makes a small classifier label substantive work as routine.
        suffix = "\n\n".join([f"Latest user instruction: {current}", *notes])
        parts = [previous, reply]
        if progress:
            parts.append("Latest progress: " + progress)
        state = "\n\n".join(part for part in parts if part)
        rendered = state[: MAX_PROMPT_CHARS - len(suffix) - 2] + "\n\n" + suffix
        return rendered, True, previous, False

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
    return rendered, True, current, False


def render_decision_input(user_message, conversation_history, *, prior_tier=None, has_visual=False):
    """Build a bounded decision snapshot from the original request and preceding transcript."""
    text, contextual, _, _ = render_turn_context(
        user_message, conversation_history, prior_tier=prior_tier, has_visual=has_visual
    )
    return text, contextual


def _fit_excerpt(text, limit):
    if len(text) <= limit:
        return text
    tail = limit // 4
    return text[: limit - tail - 3] + " … " + text[-tail:]


def render_progress_input(task, summary, current_tier=None, objective=None):
    """Reassess the remaining work using the last route and the user's objective."""
    tier = current_tier if current_tier in ROUTE_SPECS else "unknown"
    work = _fit_excerpt(objective or task, 1000)
    progress = (
        f"\n\nCurrent tier for this task: {tier}\n\nMost recent tool results:\n{summary[:1200]}"
        f"\n\nWork the user requested:\n{work}"
        "\nUse the tier needed for the remaining work. A completed tool call alone does not "
        "establish that the requested work is done."
    )
    return _fit_excerpt(task, MAX_PROMPT_CHARS - len(progress)) + progress


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
    objective: str = field(repr=False)
    resolved_reference: bool
    base_url: str | None = None
    identity: tuple[str, bytes] | None = None
    identity_change_logged: bool = False
    current: _Decision | None = None
    batches: dict[bytes, _Decision] = field(default_factory=dict)

    def close(self):
        self.text = ""
        self.objective = ""
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
        text, contextual, objective, resolved_reference = render_turn_context(
            user_message, history[:-1], prior_tier=prior_tier, has_visual=has_visual
        )
        if not text:
            return
        with self.lock:
            if key not in self.turns and len(self.turns) < MAX_TURNS:
                now = time.monotonic()
                self.turns[key] = _Turn(
                    settings,
                    sender_id,
                    session_id,
                    now + STAGED_TTL,
                    text,
                    contextual,
                    objective,
                    resolved_reference,
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
            self._start_decision(
                turn,
                decision,
                text,
                contextual=turn.contextual,
                evidence_request=(
                    turn.objective
                    if turn.resolved_reference and not _only_delegates(turn.objective)
                    else None
                ),
            )
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
                    text = render_progress_input(
                        turn.text, summary, turn.tier(current.model), turn.objective
                    )
                    break
            self._wait_for(current)
        self._start_decision(turn, decision, text, contextual=True)
        return decision

    def _wait_for(self, decision):
        if not decision.ready.wait(max(0.0, decision.deadline - time.monotonic())):
            with self.lock:
                self._resolve(decision, reason="classifier timeout")

    def _start_decision(self, turn, decision, text, *, contextual, evidence_request=None):
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
                    turn.objective = ""
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

        def keep_primary(model):
            # The optional evidence check shares the original deadline. A slow second
            # response must preserve the already verified first decision.
            with self.lock:
                if decision.ready.is_set() or time.monotonic() >= decision.deadline:
                    return False
                decision.model = model
                return True

        def classify():
            try:
                if decision.ready.is_set():
                    return
                timeout = decision.deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError
                choice = gateway.classify(
                    turn.settings,
                    text,
                    contextual=contextual,
                    timeout_s=timeout,
                    evidence_request=evidence_request,
                    on_primary=keep_primary if evidence_request else None,
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
