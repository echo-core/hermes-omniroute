import pytest

from backend.routing_policy import build_questions, select_route

ROUTES = {
    "routine": "quick-answer",
    "visual": "see-attachment",
    "bounded": "focused-work",
    "escalation": "complex-work",
}


def response(tier, *, confidence=0.83, source=0.0, advanced=0.0):
    return {
        "answers": {
            "route": {"type": "choice", "choice": tier, "confidence": confidence},
            "research_scope": {
                "type": "choice",
                "probabilities": {"source_research": source, "advanced_research": advanced},
            },
        },
    }


def test_rubric_uses_configured_tiers_and_contextual_mode():
    first_turn = build_questions(ROUTES)
    follow_up = build_questions(ROUTES, contextual=True)
    assert first_turn["route"]["criteria"] == follow_up["route"]["criteria"]
    assert list(first_turn["route"]["criteria"]) == ["routine", "bounded", "escalation", "visual"]
    assert "preceding task or plan" in follow_up["route"]["instructions"]
    assert "independent new question" in follow_up["route"]["instructions"]
    assert "preceding task or plan" not in first_turn["route"]["instructions"]
    assert "evidence needed for the newest request" in follow_up["research_scope"]["instructions"]
    assert not any(model in str(first_turn) or model in str(follow_up) for model in ROUTES.values())


def test_only_eligible_tiers_are_offered():
    routes = {tier: ROUTES[tier] for tier in ("routine", "bounded")}
    questions = build_questions(routes)
    assert list(questions["route"]["criteria"]) == ["routine", "bounded"]
    assert "Choose visual" not in questions["route"]["instructions"]
    with pytest.raises(ValueError):
        build_questions({"routine": "quick-answer"})
    with pytest.raises(ValueError):
        build_questions({"routine": "quick-answer", "other": "unlisted"})
    with pytest.raises(ValueError):
        select_route(response("escalation"), routes)
    assert (
        select_route(response("bounded", advanced=0.9), routes)["selected_model"] == "focused-work"
    )


@pytest.mark.parametrize(
    ("tier", "source", "advanced", "selected", "bucket", "confidence"),
    [
        ("routine", 0.64, 0.0, "quick-answer", "simple", 0.83),
        ("routine", 0.65, 0.0, "focused-work", "medium", 0.65),
        ("routine", 0.45, 0.2, "focused-work", "medium", 0.65),
        ("routine", 0.1, 0.7, "complex-work", "complex", 0.7),
        ("bounded", 0.1, 0.7, "complex-work", "complex", 0.7),
        ("visual", 0.1, 0.9, "see-attachment", "visual", 0.83),
        ("escalation", 0.9, 0.0, "complex-work", "complex", 0.83),
        ("routine", True, float("nan"), "quick-answer", "simple", 0.83),
    ],
)
def test_evidence_thresholds(tier, source, advanced, selected, bucket, confidence):
    result = select_route(response(tier, source=source, advanced=advanced), ROUTES)
    assert result == {"selected_model": selected, "bucket": bucket, "confidence": confidence}


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"answers": []},
        {"answers": {"route": {"type": "score", "choice": "routine", "confidence": 0.9}}},
        response(["routine"]),
        response("auto-recursive"),
        response("focused-work"),
        response("routine", confidence=True),
        response("routine", confidence=float("nan")),
        response("routine", confidence=float("inf")),
    ],
)
def test_rejects_malformed_or_unoffered_answers(result):
    with pytest.raises(ValueError):
        select_route(result, ROUTES)
