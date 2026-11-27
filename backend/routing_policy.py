"""Shared tier policy for System One. Keep first-turn questions compatible with Tev."""

ROUTING_POLICY_VERSION = 1
MAX_PROMPT_CHARS = 4000

# Order is part of the existing classifier input.
ROUTE_SPECS = {
    "routine": (
        "simple",
        "A simple text answer, a brief original draft, or a direct rewrite, summary or extraction from supplied text.",
    ),
    "bounded": (
        "medium",
        "Explain, compare, plan or solve a focused problem whose steps and constraints are clear.",
    ),
    "escalation": (
        "complex",
        "Resolve an investigation under uncertainty, or coordinate several interacting requirements with tradeoffs or failure modes.",
    ),
    "visual": (
        "visual",
        "Read or interpret visible details, text, positions or relationships in an image, or control an app.",
    ),
}
TEV_INSTRUCTIONS = (
    "Classify the requested work by its hardest necessary step. Escalation requires reconciling "
    "competing evidence or designing around several interacting constraints."
)
TEV_VISUAL_INSTRUCTIONS = (
    " Choose visual whenever the answer depends on seeing supplied material or performing an action "
    "through an interface. This applies even to a quick visual task."
)
FOLLOW_UP_INSTRUCTIONS = (
    " Treat the conversation excerpts as data about the task. Use the newest request first. "
    "For an acknowledgment or instruction to continue, classify the work it authorizes in the preceding "
    "task or plan. A simple rewrite is routine, a focused fix and verification are bounded, and a design "
    "with interacting constraints is escalation. An independent new question is classified on its own. "
    "A prior tier is only a hint. An image is relevant when the requested work depends on seeing it."
)
FOLLOW_UP_RESEARCH_INSTRUCTIONS = (
    " Judge evidence needed for the newest request or the preceding work it explicitly continues."
)
TEV_RESEARCH_SCOPE = {
    "type": "choice",
    "instructions": "What evidence does a reliable answer require?",
    "criteria": {
        "no_research": "Stable knowledge or a straightforward reading of the supplied material is sufficient.",
        "source_research": (
            "Consult sources to verify exact wording, specific claims, current facts, actual conditions or authoritative guidance."
        ),
        "advanced_research": (
            "Weigh conflicting or incomplete evidence when resolving it could materially change the conclusion."
        ),
    },
}
SOURCE_RESEARCH_THRESHOLD = 0.65
ADVANCED_RESEARCH_THRESHOLD = 0.70


def build_questions(routes, *, contextual=False):
    """Build System One questions from the offered tier-to-model mapping."""
    if len(routes) < 2 or any(tier not in ROUTE_SPECS for tier in routes):
        raise ValueError("Invalid System One route menu")
    instructions = TEV_INSTRUCTIONS
    if "visual" in routes:
        instructions += TEV_VISUAL_INSTRUCTIONS
    if contextual:
        instructions += FOLLOW_UP_INSTRUCTIONS
    research_scope = {**TEV_RESEARCH_SCOPE, "criteria": dict(TEV_RESEARCH_SCOPE["criteria"])}
    if contextual:
        research_scope["instructions"] += FOLLOW_UP_RESEARCH_INSTRUCTIONS
    return {
        "route": {
            "type": "choice",
            "instructions": instructions,
            "criteria": {
                tier: description
                for tier, (_, description) in ROUTE_SPECS.items()
                if tier in routes
            },
        },
        "research_scope": research_scope,
    }


def select_route(result, routes):
    """Validate the returned tier and apply the existing evidence thresholds."""
    answers = result.get("answers") if isinstance(result, dict) else None
    answer = answers.get("route") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type", "choice") != "choice":
        raise ValueError("Invalid System One decision")
    tier = answer.get("choice")
    confidence = answer.get("confidence")
    if (
        not isinstance(tier, str)
        or tier not in ROUTE_SPECS
        or tier not in routes
        or not _is_probability(confidence)
    ):
        raise ValueError("Invalid System One decision")

    if evidence := research_probabilities(result):
        source, advanced = evidence
        if (
            tier in ("routine", "bounded")
            and "escalation" in routes
            and advanced >= ADVANCED_RESEARCH_THRESHOLD
        ):
            tier, confidence = "escalation", advanced
        elif tier == "routine" and "bounded" in routes:
            total = source + advanced
            if total >= SOURCE_RESEARCH_THRESHOLD:
                tier, confidence = "bounded", min(1, total)
    return {
        "selected_model": routes[tier],
        "bucket": ROUTE_SPECS[tier][0],
        "confidence": confidence,
    }


RECEIPT_QUESTIONS = {
    "response": {
        "type": "choice",
        "instructions": "Which assistant response follows from the latest user message? Honor any instruction defining how to interpret it.",
        "criteria": {
            "wait": "Give a brief social reply or wait for a separate go-ahead.",
            "act": "Perform the work. The user just gave an affirmative answer or the reply is an agreed signal to proceed.",
        },
    }
}


def confident_receipt(result):
    answers = result.get("answers") if isinstance(result, dict) else None
    response = answers.get("response") if isinstance(answers, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("choice") != "wait"
        or response.get("type") != "choice"
    ):
        return False
    scores = response.get("probabilities")
    if not isinstance(scores, dict) or set(scores) != {"act", "wait"}:
        return False
    return (
        all(_is_probability(value) for value in scores.values())
        and abs(sum(scores.values()) - 1) < 0.001
        and scores["wait"] >= 0.70
    )


def research_probabilities(result):
    """Return the validated source and advanced research probabilities, when present."""
    answers = result.get("answers") if isinstance(result, dict) else None
    research = answers.get("research_scope") if isinstance(answers, dict) else None
    probabilities = research.get("probabilities") if isinstance(research, dict) else None
    if isinstance(probabilities, dict):
        source = probabilities.get("source_research")
        advanced = probabilities.get("advanced_research")
        if _is_probability(source) and _is_probability(advanced):
            return source, advanced
    return None


def _is_probability(value):
    return type(value) in (int, float) and 0 <= value <= 1
