"""Deterministic objective grading (FR-READ-04 / FR-LIST-05): normalized
text comparison with configured aliases, per-question marks. Every
question type currently modeled (multiple_choice, multiple_select,
true_false_not_given, yes_no_not_given, matching, short_answer,
sentence_completion, gap_fill, form_completion, note_completion,
table_completion) is objectively gradable - Writing and Speaking use their
own separate tables/`not_assessed` flow (Phase 3), not this one.

Rules that follow real IELTS marking:
- A text answer over the question's word limit (`options.max_words`) is
  wrong even if it contains the right words. Numbers don't count as words
  ("NO MORE THAN TWO WORDS AND/OR A NUMBER").
- Matching and multiple-select questions with several items earn marks per
  item: each correct item is worth an equal share of the question's marks.
  An extra wrong selection cancels one correct one, so ticking every option
  can't earn full marks.
- Spelling variants the exam accepts (British/American spelling, hyphens,
  "19th" for "19", "1,000" for "1000") match without an admin alias.
"""

import re
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_HYPHENS_RE = re.compile(r"[-‐-―]")
_ORDINAL_RE = re.compile(r"\b(\d+)(st|nd|rd|th)\b")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
_IZE_RE = re.compile(r"\b(\w{2,})(i|y)z(e|es|ed|ing|ation|ations)\b")
_NUMBER_RE = re.compile(r"^[\d.,:/%£$€]+$")

# Common British/American pairs that a suffix rule can't catch safely.
# Both sides of a comparison go through the same map, so a student who
# writes either spelling matches a key written in either.
_SPELLING = {
    "color": "colour",
    "colors": "colours",
    "center": "centre",
    "centers": "centres",
    "theater": "theatre",
    "theaters": "theatres",
    "meter": "metre",
    "meters": "metres",
    "liter": "litre",
    "liters": "litres",
    "favor": "favour",
    "favorite": "favourite",
    "honor": "honour",
    "labor": "labour",
    "neighbor": "neighbour",
    "neighbors": "neighbours",
    "neighborhood": "neighbourhood",
    "behavior": "behaviour",
    "flavor": "flavour",
    "harbor": "harbour",
    "humor": "humour",
    "rumor": "rumour",
    "jewelry": "jewellery",
    "catalog": "catalogue",
    "dialog": "dialogue",
    "defense": "defence",
    "license": "licence",
    "offense": "offence",
    "gray": "grey",
    "tire": "tyre",
    "tires": "tyres",
    "aluminum": "aluminium",
    "traveler": "traveller",
    "travelers": "travellers",
    "traveling": "travelling",
    "traveled": "travelled",
    "canceled": "cancelled",
    "program": "programme",
    "programs": "programmes",
}


def _normalize_text(value: str) -> str:
    text = value.strip().lower()
    text = _HYPHENS_RE.sub(" ", text)
    text = _THOUSANDS_RE.sub("", text)
    text = _ORDINAL_RE.sub(r"\1", text)
    text = _IZE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}s{m.group(3)}", text)
    text = _WHITESPACE_RE.sub(" ", text).rstrip(".,!?;:")
    return " ".join(_SPELLING.get(word, word) for word in text.split(" "))


def word_count(value: str) -> int:
    """Words as IELTS counts them: hyphenated words are one word, and
    numbers (digits, dates, prices, times) are not words."""

    return sum(1 for token in value.split() if not _NUMBER_RE.match(token))


def _deep_normalize(value: Any) -> Any:
    """Order-independent, case-insensitive structural normalization, for
    answer shapes that aren't free text (a selected option, a list of
    selected indices, a matching dict)."""

    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, list):
        return sorted((_deep_normalize(item) for item in value), key=str)
    if isinstance(value, dict):
        return {str(key): _deep_normalize(val) for key, val in sorted(value.items())}
    return value


def _max_words(options: Any) -> int | None:
    if isinstance(options, dict):
        limit = options.get("max_words")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            return limit
    return None


def _is_text_key(correct_answers: Any) -> bool:
    return isinstance(correct_answers, list) and all(isinstance(item, str) for item in correct_answers)


# The response shape each question type's input sends (QuestionRenderer in
# the frontend). A response of any other shape earns nothing: the shape
# must never pick a more generous marking rule than the question's type.
_STRING_TYPES = {
    "multiple_choice",
    "true_false_not_given",
    "yes_no_not_given",
    "short_answer",
    "sentence_completion",
    "gap_fill",
    "form_completion",
    "note_completion",
    "table_completion",
}


def _shape_ok(question_type: str | None, response: Any) -> bool:
    if question_type is None:
        return True
    if question_type in _STRING_TYPES:
        return isinstance(response, str)
    if question_type == "multiple_select":
        return isinstance(response, list)
    if question_type == "matching":
        return isinstance(response, dict)
    return True


def credit(
    response: Any,
    correct_answers: Any,
    accepted_aliases: Any | None,
    options: Any = None,
    question_type: str | None = None,
) -> float:
    """The share of a question's marks a response earns, from 0.0 to 1.0.

    Pass `question_type` (every production caller does): a response whose
    shape doesn't match the type earns 0, so a single string can't be marked
    as a text answer on a multiple-select question, and a list can't skip a
    text question's word limit."""

    if response is None or not _shape_ok(question_type, response):
        return 0.0

    if isinstance(response, str) and _is_text_key(correct_answers):
        limit = _max_words(options)
        if limit is not None and word_count(response) > limit:
            return 0.0
        accepted = {_normalize_text(item) for item in correct_answers}
        if isinstance(accepted_aliases, list):
            accepted |= {_normalize_text(item) for item in accepted_aliases if isinstance(item, str)}
        return 1.0 if _normalize_text(response) in accepted else 0.0

    # Matching with several prompts: one equal share per prompt.
    if isinstance(correct_answers, dict) and len(correct_answers) > 1 and isinstance(response, dict):
        given = {str(key): _deep_normalize(val) for key, val in response.items()}
        hits = sum(1 for key, val in correct_answers.items() if given.get(str(key)) == _deep_normalize(val))
        return hits / len(correct_answers)

    # Multiple select with several correct options: hits minus extras.
    if isinstance(correct_answers, list) and len(correct_answers) > 1 and isinstance(response, list):
        wanted = {str(_deep_normalize(item)) for item in correct_answers}
        chosen = {str(_deep_normalize(item)) for item in response}
        hits = len(wanted & chosen)
        extras = len(chosen - wanted)
        return max(0, hits - extras) / len(wanted)

    return 1.0 if _deep_normalize(response) == _deep_normalize(correct_answers) else 0.0


def is_correct(
    response: Any,
    correct_answers: Any,
    accepted_aliases: Any | None,
    options: Any = None,
    question_type: str | None = None,
) -> bool:
    """True if `response` earns full marks."""

    return credit(response, correct_answers, accepted_aliases, options, question_type) >= 1.0


def marks_for(question: Any, answer_key: Any | None, response: Any) -> float:
    """Marks a response earns on one question (0 without an answer key)."""

    if answer_key is None:
        return 0.0
    share = credit(
        response,
        answer_key.correct_answers,
        answer_key.accepted_aliases,
        getattr(question, "options", None),
        getattr(question, "type", None),
    )
    return round(question.marks * share, 2)


def grade_responses(
    questions_with_keys: list[tuple[Any, Any | None]],
    responses_by_question_id: dict[Any, Any],
) -> tuple[float, float]:
    """Returns (raw_score, max_score) across every question in a test
    version. A question with no answer key contributes 0 to max_score
    (shouldn't happen for a published version, but never divide-by-zero
    or crash over it); an unanswered question scores 0.
    """

    raw_score = 0.0
    max_score = 0.0
    for question, answer_key in questions_with_keys:
        if answer_key is None:
            continue
        max_score += question.marks
        response_row = responses_by_question_id.get(question.id)
        if response_row is None:
            continue
        raw_score += marks_for(question, answer_key, response_row.response)
    return round(raw_score, 2), max_score
