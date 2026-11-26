"""A numbered follow-up uses the requested item and preserves ambiguous context."""

import pytest

from backend.turn_routing import (
    MAX_PROMPT_CHARS,
    _numbered_item,
    _numbered_reference,
    _only_delegates,
    _render_reference,
    render_decision_input,
    render_turn_context,
)

_TASK = "Review the sample project for mistakes and describe what you find."
_FIRST = "1. **Status reporting**\n   The wrapper can mask errors."
_SECOND = "2. **Help text**\n   The synopsis is incomplete."
_ANSWER = f"Findings\n\n{_FIRST}\n\n{_SECOND}\n\nI checked both platforms."
_VERIFY = "Verify the first finding with a concrete reproduced example."


def _history(answer=_ANSWER):
    return [{"role": "user", "content": _TASK}, {"role": "assistant", "content": answer}]


@pytest.fixture
def _native_context_helpers():
    pytest.importorskip("agent.prompt_builder")


@pytest.mark.parametrize(
    ("ask", "item"),
    [
        (_VERIFY, _FIRST),
        ("Remind me what the first finding was.", _FIRST),
        ("Could you rewrite finding #2 more simply?", _SECOND),
        ("Check the 2nd finding against the source.", _SECOND),
        ("Expand on option number 2.", _SECOND),
        ("Go ahead with option two.", _SECOND),
    ],
)
def test_only_the_requested_numbered_item_is_sent(ask, item, _native_context_helpers):
    expected = f"Current request:\n{ask}\n\nPreceding assistant plan or result:\n{item}"
    assert render_turn_context(ask, _history(), prior_tier="routine") == (
        expected,
        True,
        ask,
        True,
    )
    assert render_decision_input(ask, _history(), prior_tier="routine") == (expected, True)


def test_bold_numbered_headings_keep_their_body(_native_context_helpers):
    first = "**1. Status reporting**\n\nThe wrapper can mask errors."
    answer = f"Findings\n\n{first}\n\n**2. Help text**\n\nThe synopsis is incomplete."
    text, _, _, resolved = render_turn_context(_VERIFY, _history(answer))
    assert resolved
    assert text.endswith(first)


@pytest.mark.parametrize(
    "ask",
    [
        "Verify it with an example.",
        "Verify the first finding and the second one.",
        "Verify finding 1 or 2.",
        "Verify finding 1–2.",
        "Verify the first finding and the remaining findings.",
        "Explain the third finding.",
        "New question, what is the first step?",
        "What is the first step in cooking rice?",
        "Verify the first finding. Separately, make a shopping list.",
        f"{_VERIFY} " + "More request text. " * 30,
    ],
)
def test_ambiguous_or_independent_requests_keep_broad_context(ask, _native_context_helpers):
    text, contextual, objective, resolved = render_turn_context(ask, _history())
    assert contextual and not resolved
    assert objective == ask.strip()
    assert _TASK in text
    assert _FIRST in text
    assert _SECOND in text


@pytest.mark.parametrize(
    "answer",
    [
        f"{_ANSWER}\n\n1. Another list\n2. And another entry",
        "1. First list\n\n### Second list\n\n2. In a separate block",
        "1. First list\n\nA separate introduction\n\n2. In another block",
        "1. Parent item\n   1. Nested item\n2. Second parent item",
        "1. Finding\n\n```text\n2. Code content\n```\n\n2. Another finding",
        "1. Finding\n\n       code content\n\n2. Another finding",
        "1. Finding with ![diagram](sample.png)\n2. Another finding",
        "> 1. Numbered quotation\n> 2. It is not a numbered plan",
        "1. " + "X" * 1500 + "\n2. Another finding",
        f"{_ANSWER}\n\n" + "X" * MAX_PROMPT_CHARS,
    ],
)
def test_uncertain_item_boundaries_keep_broad_context(answer, _native_context_helpers):
    text, contextual, objective, resolved = render_turn_context(_VERIFY, _history(answer))
    assert contextual and not resolved
    assert objective == _VERIFY
    assert _TASK in text
    assert len(text) <= MAX_PROMPT_CHARS


def test_only_the_immediately_preceding_relevant_answer_can_supply_an_item(_native_context_helpers):
    history = _history() + [
        {"role": "user", "content": "Explain that more simply."},
        {"role": "assistant", "content": "The output can look successful after an error."},
    ]
    assert not render_turn_context(_VERIFY, history)[-1]


def test_tool_rows_and_tool_call_preambles_are_not_answers(_native_context_helpers):
    history = _history() + [
        {"role": "assistant", "content": "1. Tool preamble", "tool_calls": [{"id": "fixture"}]},
        {"role": "tool", "content": "1. Tool output"},
    ]
    text, _, _, resolved = render_turn_context(_VERIFY, history)
    assert resolved
    assert text.endswith(_FIRST)


def test_selected_item_uses_sanitized_text(_native_context_helpers):
    first = "1. **Prior result**\n   A safe excerpt."
    answer = (
        "1. **Prior result**\n"
        "   <memory-context>WHAT_WAS_RECALLED</memory-context>A safe excerpt.\n"
        "2. **Other result**"
    )
    text, _, objective, resolved = render_turn_context(_VERIFY, _history(answer))
    assert resolved and objective == _VERIFY
    assert text.endswith(first)
    assert "WHAT_WAS_RECALLED" not in text


def test_visual_requests_keep_their_visual_hint(_native_context_helpers):
    text, contextual, _, resolved = render_turn_context(_VERIFY, _history(), has_visual=True)
    assert contextual and not resolved
    assert "Visual input is present in the current request." in text
    history = _history()
    history[0]["content"] = [{"type": "image_url", "image_url": {"url": "fixture"}}]
    text, contextual, _, resolved = render_turn_context(_VERIFY, history)
    assert contextual and not resolved
    assert "Visual input is present in the preceding task." in text


def test_bare_approvals_keep_preceding_work_and_first_turns_keep_plain_text(
    _native_context_helpers,
):
    text, contextual, objective, resolved = render_turn_context("Go ahead.", _history())
    assert contextual and not resolved
    assert objective == _TASK
    assert text.startswith(_TASK)
    assert text.endswith("Latest user instruction: Go ahead.")
    assert render_turn_context(_VERIFY, []) == (_VERIFY, False, _VERIFY, False)


@pytest.mark.parametrize(
    ("ask", "delegation"),
    [
        ("Do the first one", True),
        ("Please do the second item.", True),
        ("Go ahead with option two.", True),
        ("Do the first one and check the source.", False),
        ("Verify the first finding.", False),
        ("Do that.", False),
    ],
)
def test_only_delegates(ask, delegation):
    assert _only_delegates(ask) is delegation


@pytest.mark.parametrize(
    "ask",
    [
        "Check the fırst finding.",
        "Check item sıx.",
        "Show the first example from Python's docs.",
        "Show me the first finding on a topic we haven't discussed.",
        "Tell me about the first example about quantum computing.",
        "Compare option 1 vs. 2.",
        "Compare option 1 or the last one.",
    ],
)
def test_near_matches_keep_the_broad_context(ask):
    assert _numbered_reference(ask) is None


@pytest.mark.parametrize(
    ("ask", "expected"),
    [
        ("Do the second item.", "Do “Draw a diagram”."),
        (
            "Put the second point in one short sentence.",
            "Put “Draw a diagram” in one short sentence.",
        ),
        (
            "Explain the second example from your previous answer.",
            "Explain “Draw a diagram” from your previous answer.",
        ),
    ],
)
def test_short_plain_items_are_inlined(ask, expected):
    number, match = _numbered_reference(ask)
    item = _numbered_item("1. Take notes.\n2. Draw a diagram.", number)
    assert _render_reference(ask, match, item) == expected


@pytest.mark.parametrize("body", ["Check the `README`.", "Check https://example.com.", "X" * 241])
def test_rich_or_long_item_keeps_its_own_section(body):
    ask = "Do the first one."
    number, match = _numbered_reference(ask)
    item = f"1. {body}"
    assert _numbered_item(item, number) == item
    assert _render_reference(ask, match, item).endswith(
        f"Preceding assistant plan or result:\n{item}"
    )
