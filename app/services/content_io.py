"""Whole-test JSON import/export, the admin content tree, and publish
validation (FR-ADMIN-03/04/08).

One document shape - `prepband-test/v1` - is used for all of:
  * POST /admin/tests/import   (admin UI "Import JSON")
  * GET  /admin/tests/{id}/export
  * POST /admin/tests/{id}/duplicate (export + import as a new draft)
  * scripts/seed_content.py    (loads database/seeds/*.json)

so seeds, exports and hand-written imports are interchangeable.

Audio in an imported document: a media asset with no `storage_key` is
created as a *pending* placeholder (storage_key `pending/<uuid>`, no
public_url) so its transcript and attribution survive import. The admin
UI then uploads the real file through the `upload-test-media` Edge
Function and PATCHes the asset's storage_key/public_url. validate_for_publish
refuses to publish a Listening section until that has happened.
"""

import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session, select

from ..models import AnswerKey, MediaAsset, Passage, Question, Section, SpeakingPrompt, Test, TestVersion, WritingPrompt
from ..schemas.content import FollowUp, MediaType, Module, QuestionType, TestKind, Track

FORMAT = "prepband-test/v1"
PENDING_PREFIX = "pending/"

TEXT_TYPES = {
    "short_answer",
    "sentence_completion",
    "gap_fill",
    "form_completion",
    "note_completion",
    "table_completion",
}


# --- document schema -----------------------------------------------------


class AnswerDoc(BaseModel):
    correct_answers: Any
    accepted_aliases: list[str] | None = None
    rationale: str | None = None


class QuestionDoc(BaseModel):
    type: QuestionType
    prompt: str = Field(min_length=1)
    options: Any | None = None
    marks: float = Field(default=1, ge=0)
    answer: AnswerDoc | None = None


def _join_paragraphs(value: Any) -> Any:
    """Long text may be authored as a list of paragraphs (easier to read
    and diff in seed files); it's stored as one string, blank-line
    separated, which the frontend splits back into paragraphs."""

    if isinstance(value, list):
        return "\n\n".join(str(item).strip() for item in value if str(item).strip())
    return value


class PassageDoc(BaseModel):
    title: str | None = None
    body: str = Field(min_length=1)
    source: str | None = None
    license: str | None = None
    attribution: str | None = None

    _join_body = field_validator("body", mode="before")(_join_paragraphs)


class MediaDoc(BaseModel):
    type: MediaType = "audio"
    storage_key: str | None = None
    public_url: str | None = None
    content_type: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    attribution: str | None = None
    license: str | None = None
    transcript: str | None = None

    _join_transcript = field_validator("transcript", mode="before")(_join_paragraphs)

    # Informational only (e.g. the generated audio file a seed expects to
    # be uploaded for this asset) - never stored.
    local_file: str | None = None


class VisualDoc(BaseModel):
    """Academic Task 1 data, rendered as an accessible table."""

    caption: str | None = None
    columns: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(min_length=1)


class WritingPromptDoc(BaseModel):
    task_number: Literal[1, 2]
    prompt: str = Field(min_length=1)
    instructions: str | None = None
    min_words: int = Field(default=150, ge=0, le=2000)
    recommended_minutes: int | None = Field(default=None, ge=1, le=180)
    visual: VisualDoc | None = None
    letter_format: bool = False
    examiner_notes: str | None = None


class SpeakingPromptDoc(BaseModel):
    part: Literal[1, 2, 3]
    prompt: str = Field(min_length=1)
    bullets: list[str] | None = None
    prep_seconds: int | None = Field(default=None, ge=0, le=600)
    speak_seconds: int | None = Field(default=None, ge=1, le=600)
    # Phase 5: follow-ups for the live examiner; audio is optional and is
    # usually generated later in the admin panel.
    follow_ups: list[FollowUp] = Field(default_factory=list, max_length=10)
    audio_url: str | None = Field(default=None, pattern=r"^https://", max_length=1000)


class SectionDoc(BaseModel):
    module: Module
    instructions: str | None = None
    timing_seconds: int | None = Field(default=None, ge=0)
    passages: list[PassageDoc] = []
    media_assets: list[MediaDoc] = []
    questions: list[QuestionDoc] = []
    writing_prompts: list[WritingPromptDoc] = []
    speaking_prompts: list[SpeakingPromptDoc] = []


class TestDoc(BaseModel):
    format: Literal["prepband-test/v1"] = FORMAT
    title: str = Field(min_length=1, max_length=200)
    module: Module
    track: Track
    kind: TestKind = "practice"
    difficulty: str | None = Field(default=None, max_length=40)
    duration_minutes: int | None = Field(default=None, ge=1)
    sections: list[SectionDoc] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_questions(self) -> "TestDoc":
        problems: list[str] = []
        for s_index, section in enumerate(self.sections, start=1):
            for q_index, question in enumerate(section.questions, start=1):
                problem = question_problem(question.type, question.options, question.answer.correct_answers if question.answer else None)
                if problem:
                    problems.append(f"Section {s_index}, question {q_index}: {problem}")
        if problems:
            raise ValueError("; ".join(problems[:10]))
        return self


# --- type-specific validation (FR-ADMIN-04) ------------------------------


def question_problem(qtype: str, options: Any, correct_answers: Any) -> str | None:
    """Returns a human-readable problem with a question's options/answer
    shape, or None if it's consistent. `correct_answers` may be None (no
    key yet) - that's a publish-time problem, not an authoring one."""

    if qtype in ("multiple_choice", "multiple_select"):
        if not isinstance(options, list) or len(options) < 2 or not all(isinstance(o, str) and o.strip() for o in options):
            return "needs at least two non-empty options."
        if len(set(options)) != len(options):
            return "options must be unique."
        if correct_answers is None:
            return None
        chosen = correct_answers if isinstance(correct_answers, list) else [correct_answers]
        if not chosen or any(c not in options for c in chosen):
            return "answer must be one of the options."
        if qtype == "multiple_choice" and len(chosen) != 1:
            return "multiple choice takes exactly one correct option."
        return None

    if qtype in ("true_false_not_given", "yes_no_not_given"):
        if correct_answers is None:
            return None
        allowed = {"True", "False", "Not Given"} if qtype == "true_false_not_given" else {"Yes", "No", "Not Given"}
        value = correct_answers[0] if isinstance(correct_answers, list) and len(correct_answers) == 1 else correct_answers
        if not isinstance(value, str) or value not in allowed:
            return f"answer must be one of {', '.join(sorted(allowed))}."
        return None

    if qtype == "matching":
        if not isinstance(options, dict) or not isinstance(options.get("prompts"), list) or not isinstance(options.get("choices"), list):
            return "matching needs options {prompts: [...], choices: [...]}."
        if not options["prompts"] or not options["choices"]:
            return "matching needs at least one prompt and one choice."
        if correct_answers is None:
            return None
        if not isinstance(correct_answers, dict):
            return "matching answer must map prompt index to choice."
        for index in range(len(options["prompts"])):
            value = correct_answers.get(str(index))
            if value not in options["choices"]:
                return f"matching prompt {index + 1} has no valid answer."
        return None

    if qtype in TEXT_TYPES:
        if options is not None:
            if not isinstance(options, dict):
                return "text questions take options only as {max_words: n}."
            limit = options.get("max_words")
            if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10):
                return "max_words must be a whole number from 1 to 10."
        if correct_answers is None:
            return None
        if not isinstance(correct_answers, list) or not correct_answers or not all(isinstance(a, str) and a.strip() for a in correct_answers):
            return "text answers must be a non-empty list of accepted strings."
        return None

    return None


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_LIMIT_RE = re.compile(r"\b(?:no more than (one|two|three|four|five) words?|(one) word only)\b", re.IGNORECASE)


def word_limit_from_instructions(instructions: str | None) -> int | None:
    """The word limit a section's instructions set for its text answers
    ("NO MORE THAN TWO WORDS", "ONE WORD ONLY"), or None when there's none
    or the instructions name more than one different limit."""

    if not instructions:
        return None
    limits = {_NUMBER_WORDS[(a or b).lower()] for a, b in _LIMIT_RE.findall(instructions)}
    return limits.pop() if len(limits) == 1 else None


def with_word_limit(qtype: str, options: Any, instructions: str | None) -> Any:
    """Adds the section's word limit to a text question that has none, so
    grading enforces what the instructions tell the student."""

    if qtype not in TEXT_TYPES or (isinstance(options, dict) and "max_words" in options):
        return options
    limit = word_limit_from_instructions(instructions)
    if limit is None:
        return options
    return {**(options if isinstance(options, dict) else {}), "max_words": limit}


def normalize_answer(qtype: str, correct_answers: Any) -> Any:
    """Coerces convenient authoring shapes into what grading.is_correct
    compares against: single-answer types always become a one-item list."""

    if qtype in ("multiple_choice", "true_false_not_given", "yes_no_not_given") and isinstance(correct_answers, str):
        return [correct_answers]
    if qtype in TEXT_TYPES and isinstance(correct_answers, str):
        return [correct_answers]
    return correct_answers


# --- import ----------------------------------------------------------------


def import_test(session: Session, doc: TestDoc, created_by: UUID | None = None) -> Test:
    """Creates a new *draft* test + version 1 + all nested content from a
    document, in the caller's transaction. Never publishes - publishing
    always goes through validate_for_publish."""

    now = datetime.now(timezone.utc)
    test = Test(
        id=uuid4(),
        title=doc.title,
        module=doc.module,
        track=doc.track,
        kind=doc.kind,
        difficulty=doc.difficulty,
        duration_minutes=doc.duration_minutes,
        status="draft",
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    # Same explicit-flush ordering as admin.create_test: tests and
    # test_versions FK each other.
    session.add(test)
    session.flush()
    version = TestVersion(id=uuid4(), test_id=test.id, version_number=1, status="draft", created_at=now)
    session.add(version)
    session.flush()
    test.current_version_id = version.id
    session.add(test)

    for s_index, section_doc in enumerate(doc.sections):
        section = Section(
            id=uuid4(),
            test_version_id=version.id,
            order_index=s_index,
            module=section_doc.module,
            instructions=section_doc.instructions,
            timing_seconds=section_doc.timing_seconds,
            created_at=now,
        )
        session.add(section)
        session.flush()

        for passage_doc in section_doc.passages:
            session.add(Passage(id=uuid4(), section_id=section.id, created_at=now, **passage_doc.model_dump()))

        for media_doc in section_doc.media_assets:
            media_id = uuid4()
            data = media_doc.model_dump(exclude={"local_file"})
            if not data.get("storage_key"):
                data["storage_key"] = f"{PENDING_PREFIX}{media_id}"
                data["public_url"] = None
            session.add(MediaAsset(id=media_id, section_id=section.id, created_at=now, **data))

        for q_index, question_doc in enumerate(section_doc.questions):
            question = Question(
                id=uuid4(),
                section_id=section.id,
                type=question_doc.type,
                prompt=question_doc.prompt,
                options=with_word_limit(question_doc.type, question_doc.options, section_doc.instructions),
                display_order=q_index,
                marks=question_doc.marks,
                created_at=now,
            )
            session.add(question)
            session.flush()
            if question_doc.answer is not None:
                session.add(
                    AnswerKey(
                        id=uuid4(),
                        question_id=question.id,
                        correct_answers=normalize_answer(question_doc.type, question_doc.answer.correct_answers),
                        accepted_aliases=question_doc.answer.accepted_aliases,
                        rationale=question_doc.answer.rationale,
                        created_at=now,
                    )
                )

        for p_index, prompt_doc in enumerate(section_doc.writing_prompts):
            data = prompt_doc.model_dump()
            session.add(WritingPrompt(id=uuid4(), section_id=section.id, display_order=p_index, created_at=now, **data))

        for p_index, prompt_doc in enumerate(section_doc.speaking_prompts):
            session.add(
                SpeakingPrompt(id=uuid4(), section_id=section.id, display_order=p_index, created_at=now, **prompt_doc.model_dump())
            )

    session.flush()
    return test


# --- content tree / export -------------------------------------------------


def load_tree(session: Session, version_id: UUID) -> list[dict[str, Any]]:
    """Every section of a version with its passages, media, questions and
    answer keys - admin-only (includes answers and transcripts)."""

    sections = session.exec(
        select(Section).where(Section.test_version_id == version_id).order_by(Section.order_index)
    ).all()
    section_ids = [s.id for s in sections]
    if not section_ids:
        return []

    passages = session.exec(select(Passage).where(Passage.section_id.in_(section_ids)).order_by(Passage.created_at)).all()
    media = session.exec(select(MediaAsset).where(MediaAsset.section_id.in_(section_ids)).order_by(MediaAsset.created_at)).all()
    questions = session.exec(
        select(Question).where(Question.section_id.in_(section_ids)).order_by(Question.display_order)
    ).all()
    keys = {}
    if questions:
        keys = {
            k.question_id: k
            for k in session.exec(select(AnswerKey).where(AnswerKey.question_id.in_([q.id for q in questions]))).all()
        }

    writing = session.exec(
        select(WritingPrompt).where(WritingPrompt.section_id.in_(section_ids)).order_by(WritingPrompt.display_order)
    ).all()
    speaking = session.exec(
        select(SpeakingPrompt).where(SpeakingPrompt.section_id.in_(section_ids)).order_by(SpeakingPrompt.display_order)
    ).all()

    tree = []
    for section in sections:
        tree.append(
            {
                "section": section,
                "passages": [p for p in passages if p.section_id == section.id],
                "media_assets": [m for m in media if m.section_id == section.id],
                "questions": [(q, keys.get(q.id)) for q in questions if q.section_id == section.id],
                "writing_prompts": [w for w in writing if w.section_id == section.id],
                "speaking_prompts": [sp for sp in speaking if sp.section_id == section.id],
            }
        )
    return tree


def export_test(session: Session, test: Test) -> dict[str, Any]:
    tree = load_tree(session, test.current_version_id) if test.current_version_id else []
    return {
        "format": FORMAT,
        "title": test.title,
        "module": test.module,
        "track": test.track,
        "kind": test.kind,
        "difficulty": test.difficulty,
        "duration_minutes": test.duration_minutes,
        "sections": [
            {
                "module": node["section"].module,
                "instructions": node["section"].instructions,
                "timing_seconds": node["section"].timing_seconds,
                "passages": [
                    {"title": p.title, "body": p.body, "source": p.source, "license": p.license, "attribution": p.attribution}
                    for p in node["passages"]
                ],
                "media_assets": [
                    {
                        "type": m.type,
                        # A pending placeholder exports as "no file yet".
                        "storage_key": None if m.storage_key.startswith(PENDING_PREFIX) else m.storage_key,
                        "public_url": m.public_url,
                        "content_type": m.content_type,
                        "duration_seconds": m.duration_seconds,
                        "size_bytes": m.size_bytes,
                        "attribution": m.attribution,
                        "license": m.license,
                        "transcript": m.transcript,
                    }
                    for m in node["media_assets"]
                ],
                "questions": [
                    {
                        "type": q.type,
                        "prompt": q.prompt,
                        "options": q.options,
                        "marks": q.marks,
                        "answer": (
                            {"correct_answers": k.correct_answers, "accepted_aliases": k.accepted_aliases, "rationale": k.rationale}
                            if k
                            else None
                        ),
                    }
                    for q, k in node["questions"]
                ],
                "writing_prompts": [
                    {
                        "task_number": w.task_number,
                        "prompt": w.prompt,
                        "instructions": w.instructions,
                        "min_words": w.min_words,
                        "recommended_minutes": w.recommended_minutes,
                        "visual": w.visual,
                        "letter_format": w.letter_format,
                        "examiner_notes": w.examiner_notes,
                    }
                    for w in node["writing_prompts"]
                ],
                "speaking_prompts": [
                    {
                        "part": sp.part,
                        "prompt": sp.prompt,
                        "bullets": sp.bullets,
                        "prep_seconds": sp.prep_seconds,
                        "speak_seconds": sp.speak_seconds,
                        "follow_ups": sp.follow_ups or [],
                        "audio_url": sp.audio_url,
                    }
                    for sp in node["speaking_prompts"]
                ],
            }
            for node in tree
        ],
    }


# --- publish validation (FR-ADMIN-03) --------------------------------------


def validate_for_publish(session: Session, test: Test) -> list[str]:
    """Everything that would make a published test broken or ungradable.
    Empty list means it's safe to publish."""

    if test.current_version_id is None:
        return ["Test has no version to publish."]
    tree = load_tree(session, test.current_version_id)
    if not tree:
        return ["Add at least one section."]

    problems: list[str] = []
    for index, node in enumerate(tree, start=1):
        section = node["section"]
        label = f"Section {index}"
        # Every section must match its test: an objective (Reading/Listening)
        # test with only a Writing section would otherwise skip the question
        # checks and publish with a maximum score of zero.
        if section.module != test.module:
            problems.append(f"{label} must be a {test.module.capitalize()} section in a {test.module.capitalize()} test.")
            continue
        if section.module == "writing":
            if not node["writing_prompts"]:
                problems.append(f"{label} is Writing but has no writing task.")
            for w_index, prompt in enumerate(node["writing_prompts"], start=1):
                if prompt.visual is not None and not _visual_ok(prompt.visual):
                    problems.append(f"{label}, task {w_index}: the data table needs columns and at least one row.")
            continue
        if section.module == "speaking":
            if not node["speaking_prompts"]:
                problems.append(f"{label} is Speaking but has no prompts.")
            part_two = [p for p in node["speaking_prompts"] if p.part == 2]
            # Part 2 = one cue card (with bullets) plus optional short follow-ups.
            if part_two and not any(p.bullets for p in part_two):
                problems.append(f"{label} has Part 2 prompts but no cue card. Add bullet points to the main Part 2 prompt.")
            continue
        if not node["questions"]:
            problems.append(f"{label} has no questions.")
        if section.module == "reading" and not node["passages"]:
            problems.append(f"{label} is Reading but has no passage.")
        if section.module == "listening":
            audio = [m for m in node["media_assets"] if m.type == "audio"]
            if not audio:
                problems.append(f"{label} is Listening but has no audio.")
            elif any(not m.public_url or m.storage_key.startswith(PENDING_PREFIX) for m in audio):
                problems.append(f"{label} has audio that hasn't been uploaded yet.")
        for q_index, (question, key) in enumerate(node["questions"], start=1):
            if key is None:
                problems.append(f"{label}, question {q_index} has no answer key.")
                continue
            problem = question_problem(question.type, question.options, key.correct_answers)
            if problem:
                problems.append(f"{label}, question {q_index}: {problem}")

    # A full module mock has to be complete (spec 4.2: "two Writing tasks,
    # and a three-part Speaking script").
    if test.kind == "mock" and test.module == "writing":
        tasks = {w.task_number for node in tree for w in node["writing_prompts"]}
        if tasks != {1, 2}:
            problems.append("A Writing mock needs both Task 1 and Task 2.")
    if test.kind == "mock" and test.module == "speaking":
        parts = {sp.part for node in tree for sp in node["speaking_prompts"]}
        if parts != {1, 2, 3}:
            missing = ", ".join(str(p) for p in sorted({1, 2, 3} - parts))
            problems.append(f"A Speaking mock needs all three parts (missing Part {missing}).")
    return problems


def _visual_ok(visual: Any) -> bool:
    return (
        isinstance(visual, dict)
        and isinstance(visual.get("columns"), list)
        and len(visual["columns"]) > 0
        and isinstance(visual.get("rows"), list)
        and len(visual["rows"]) > 0
    )
