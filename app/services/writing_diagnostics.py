"""Objective Writing diagnostics (FR-WRITE-04). Deliberately *not* a
score: every value here is something a student can count for themselves
(words, paragraphs, sentence lengths), plus a few rule-based observations
with their limitations stated (FR-WRITE-08). FR-WRITE-07 forbids turning
any of this into a band - the attempt's assessment_status stays
'not_assessed'.

The frontend shows the same word count live while typing
(src/lib/writingStats.ts mirrors count_words); the server recomputes on
every save and at submission so the stored figure is authoritative.
"""

import re
from typing import Any

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

# A small, fixed list of linking words - counted, not judged. Their mere
# presence says nothing about cohesion quality; this is a prompt to reflect.
LINKING_WORDS = [
    "however", "therefore", "moreover", "furthermore", "although", "whereas",
    "consequently", "in addition", "on the other hand", "for example",
    "for instance", "in contrast", "as a result", "nevertheless", "overall",
    "in conclusion", "firstly", "secondly", "finally", "meanwhile",
]


def count_words(text: str) -> int:
    """Words are runs of letters/digits, with internal apostrophes or
    hyphens kept together ("don't", "well-known" = 1 word each), the same
    convention IELTS candidates are told to use."""

    return len(_WORD_RE.findall(text or ""))


def diagnose(text: str, min_words: int, letter_format: bool = False) -> dict[str, Any]:
    text = text or ""
    words = _WORD_RE.findall(text)
    word_count = len(words)
    paragraphs = [p for p in _PARAGRAPH_SPLIT_RE.split(text.strip()) if p.strip()] if text.strip() else []
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if _WORD_RE.search(s)] if text.strip() else []
    sentence_lengths = [count_words(s) for s in sentences]
    lowered = [w.lower() for w in words]
    unique_words = len(set(lowered))
    long_words = sum(1 for w in words if len(w) >= 7)
    lower_text = text.lower()
    linking_used = sorted({phrase for phrase in LINKING_WORDS if re.search(rf"\b{re.escape(phrase)}\b", lower_text)})

    observations: list[str] = []
    if word_count == 0:
        observations.append("No text was submitted for this task.")
    elif word_count < min_words:
        observations.append(
            f"This response has {word_count} words, below the recommended minimum of {min_words}. "
            "Short responses usually cannot cover the task fully."
        )
    if word_count > 0 and len(paragraphs) < 2:
        observations.append("The response is a single paragraph. Separate your ideas into paragraphs with a blank line between them.")
    if sentence_lengths and max(sentence_lengths) > 45:
        observations.append("At least one sentence is over 45 words long. Very long sentences can be hard to follow; check it reads clearly.")
    if word_count >= 50 and not linking_used:
        observations.append("No common linking words were found. Linking words aren't required, but check that your ideas connect clearly.")

    letter: dict[str, bool] | None = None
    if letter_format:
        first_line = text.strip().splitlines()[0].strip().lower() if text.strip() else ""
        last_lines = " ".join(text.strip().splitlines()[-3:]).lower() if text.strip() else ""
        letter = {
            "has_greeting": first_line.startswith("dear"),
            "has_sign_off": bool(re.search(r"\b(yours (sincerely|faithfully|truly)|best (wishes|regards)|kind regards|regards|many thanks)\b", last_lines)),
        }
        if word_count > 0 and not letter["has_greeting"]:
            observations.append('A letter usually opens with a greeting such as "Dear Sir or Madam," or "Dear Alex,".')
        if word_count > 0 and not letter["has_sign_off"]:
            observations.append('A letter usually closes with a sign-off such as "Yours faithfully," or "Best wishes,".')

    return {
        "word_count": word_count,
        "min_words": min_words,
        "meets_minimum": word_count >= min_words,
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "average_sentence_length": round(sum(sentence_lengths) / len(sentence_lengths), 1) if sentence_lengths else 0,
        "longest_sentence_words": max(sentence_lengths) if sentence_lengths else 0,
        "unique_word_ratio": round(unique_words / word_count, 2) if word_count else 0,
        "long_word_count": long_words,
        "linking_words_used": linking_used,
        "letter": letter,
        "observations": observations,
        "limitations": (
            "These are counts and simple rule-based checks, not an assessment. They cannot judge how well you answered "
            "the task, how clear your argument is, or how accurate your grammar and vocabulary are."
        ),
    }
