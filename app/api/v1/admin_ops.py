"""Phase 6 admin operations (FR-ADMIN-08): audit trail, bulk editing, CSV
import/export and question analytics.

  GET   /admin/audit                              audit log, newest first, filterable
  POST  /admin/tests/bulk                         publish/archive/restore/delete many tests
  PATCH /admin/sections/{id}/questions/bulk       set marks / delete / renumber many questions
  GET   /admin/sections/{id}/questions.csv        questions + answer keys as CSV
  POST  /admin/sections/{id}/questions/import-csv add (or replace) questions from CSV
  GET   /admin/tests/{id}/analytics               per-question correct rates, completion
  GET   /admin/analytics                          platform-wide activity summary

Everything here is admin-only and logged by the audit middleware.
"""

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from ...core.roles import require_admin
from ...db.session import get_session
from ...models import AdminAuditLog, AnswerKey, Attempt, Profile, Question, Section, Test, TestVersion
from ...models import Response as ResponseRow
from ...services.audit import audit_note
from ...services.content_io import TEXT_TYPES, normalize_answer, question_problem, with_word_limit
from ...services.grading import is_correct
from ...services.rate_limit import admin_bulk_limiter, per_user
from . import admin as admin_routes
from .admin_content import _get_section_or_404, _require_section_version_draft

router = APIRouter()

QUESTION_TYPES = {
    "multiple_choice",
    "multiple_select",
    "true_false_not_given",
    "yes_no_not_given",
    "matching",
    *TEXT_TYPES,
}
CSV_COLUMNS = ["display_order", "type", "prompt", "marks", "options", "correct_answers", "accepted_aliases", "rationale"]
MAX_CSV_ROWS = 500
MAX_CSV_CHARS = 1_000_000
FINISHED = ("submitted", "expired")


# --- audit log -----------------------------------------------------------


class AuditEntryOut(BaseModel):
    id: str
    actor_id: str | None
    actor_email: str | None
    actor_role: str | None
    action: str
    method: str
    path: str
    entity_type: str | None
    entity_id: str | None
    outcome: str
    status_code: int
    details: dict[str, Any]
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    total: int
    limit: int
    offset: int


@router.get("/admin/audit", response_model=AuditPage)
def list_audit_log(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    outcome: Literal["success", "denied", "failed"] | None = None,
    entity_type: str | None = Query(default=None, max_length=60),
    entity_id: str | None = Query(default=None, max_length=100),
    actor_id: UUID | None = None,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AuditPage:
    filters = []
    if outcome:
        filters.append(AdminAuditLog.outcome == outcome)
    if entity_type:
        filters.append(AdminAuditLog.entity_type == entity_type)
    if entity_id:
        filters.append(AdminAuditLog.entity_id == entity_id)
    if actor_id:
        filters.append(AdminAuditLog.actor_id == actor_id)

    total = session.exec(select(func.count()).select_from(AdminAuditLog).where(*filters)).one()
    rows = session.exec(
        select(AdminAuditLog).where(*filters).order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(limit)
    ).all()
    actor_ids = {row.actor_id for row in rows if row.actor_id}
    emails = (
        dict(session.exec(select(Profile.id, Profile.email).where(Profile.id.in_(actor_ids))).all()) if actor_ids else {}
    )
    items = [
        AuditEntryOut(
            id=str(row.id),
            actor_id=str(row.actor_id) if row.actor_id else None,
            actor_email=emails.get(row.actor_id),
            actor_role=row.actor_role,
            action=row.action,
            method=row.method,
            path=row.path,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            outcome=row.outcome,
            status_code=row.status_code,
            details=row.details or {},
            created_at=row.created_at,
        )
        for row in rows
    ]
    return AuditPage(items=items, total=total, limit=limit, offset=offset)


# --- bulk test actions ---------------------------------------------------


class BulkTestAction(BaseModel):
    action: Literal["publish", "archive", "restore", "delete"]
    test_ids: list[UUID] = Field(min_length=1, max_length=100)


class BulkItemResult(BaseModel):
    id: str
    ok: bool
    status: str | None = None
    error: str | None = None
    problems: list[str] = []


class BulkResult(BaseModel):
    action: str
    succeeded: int
    failed: int
    results: list[BulkItemResult]


def _error_text(detail: Any) -> tuple[str, list[str]]:
    if isinstance(detail, dict):
        return str(detail.get("message", "Failed.")), [str(p) for p in detail.get("problems", [])]
    return str(detail), []


@router.post("/admin/tests/bulk", response_model=BulkResult, dependencies=[Depends(per_user(admin_bulk_limiter))])
def bulk_test_action(
    body: BulkTestAction,
    request: Request,
    role: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> BulkResult:
    """Applies one lifecycle action to many tests. Each test is handled on
    its own (the same rules as the single-test endpoints), so one test that
    can't be published doesn't stop the rest."""

    handlers = {
        "publish": admin_routes.publish_test,
        "archive": admin_routes.archive_test,
        "restore": admin_routes.restore_test,
        "delete": admin_routes.delete_test,
    }
    handler = handlers[body.action]
    results: list[BulkItemResult] = []
    for test_id in dict.fromkeys(body.test_ids):  # de-duplicate, keep order
        try:
            out = handler(test_id=test_id, _=role, session=session)
            results.append(BulkItemResult(id=str(test_id), ok=True, status=getattr(out, "status", None) or "deleted"))
        except HTTPException as exc:
            session.rollback()
            message, problems = _error_text(exc.detail)
            results.append(BulkItemResult(id=str(test_id), ok=False, error=message, problems=problems))
    succeeded = sum(1 for r in results if r.ok)
    audit_note(request, bulk_action=body.action, test_ids=[r.id for r in results], succeeded=succeeded)
    return BulkResult(action=body.action, succeeded=succeeded, failed=len(results) - succeeded, results=results)


# --- bulk question edits -------------------------------------------------


class BulkQuestionEdit(BaseModel):
    action: Literal["set_marks", "delete", "renumber"]
    question_ids: list[UUID] = Field(default_factory=list, max_length=500)
    marks: float | None = Field(default=None, ge=0, le=100)


@router.patch("/admin/sections/{section_id}/questions/bulk")
def bulk_edit_questions(
    section_id: UUID,
    body: BulkQuestionEdit,
    request: Request,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Draft sections only. `renumber` sets display_order 1..n in the
    current order and ignores question_ids."""

    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)
    questions = session.exec(
        select(Question).where(Question.section_id == section_id).order_by(Question.display_order, Question.created_at)
    ).all()

    if body.action == "renumber":
        for index, question in enumerate(questions, start=1):
            question.display_order = index
            session.add(question)
        session.commit()
        audit_note(request, bulk_action="renumber", count=len(questions))
        return {"action": "renumber", "updated": len(questions)}

    if not body.question_ids:
        raise HTTPException(status_code=422, detail="Choose at least one question.")
    by_id = {q.id: q for q in questions}
    missing = [str(qid) for qid in body.question_ids if qid not in by_id]
    if missing:
        raise HTTPException(status_code=422, detail={"message": "Some questions aren't in this section.", "problems": missing})

    if body.action == "set_marks":
        if body.marks is None:
            raise HTTPException(status_code=422, detail="Give the marks to set.")
        for qid in body.question_ids:
            by_id[qid].marks = body.marks
            session.add(by_id[qid])
    else:
        for qid in body.question_ids:
            key = session.exec(select(AnswerKey).where(AnswerKey.question_id == qid)).first()
            if key is not None:
                session.delete(key)
            session.delete(by_id[qid])
    session.commit()
    audit_note(request, bulk_action=body.action, question_ids=[str(q) for q in body.question_ids], marks=body.marks)
    return {"action": body.action, "updated": len(body.question_ids)}


# --- CSV import / export -------------------------------------------------


def _cell_out(value: Any) -> str:
    """Lists of plain strings become `a|b|c`; anything else is JSON."""

    if value is None:
        return ""
    if isinstance(value, list) and all(isinstance(v, str) and "|" not in v for v in value):
        return "|".join(value)
    return json.dumps(value, ensure_ascii=False)


def _cell_in(value: str | None) -> Any:
    text = (value or "").strip()
    if not text:
        return None
    if text[0] in "[{":
        return json.loads(text)
    return [part.strip() for part in text.split("|") if part.strip()]


@router.get("/admin/sections/{section_id}/questions.csv")
def export_questions_csv(
    section_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    section = _get_section_or_404(session, section_id)
    rows = session.exec(
        select(Question, AnswerKey)
        .join(AnswerKey, AnswerKey.question_id == Question.id, isouter=True)
        .where(Question.section_id == section.id)
        .order_by(Question.display_order)
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for question, key in rows:
        writer.writerow(
            [
                question.display_order,
                question.type,
                question.prompt,
                question.marks,
                _cell_out(question.options),
                _cell_out(key.correct_answers if key else None),
                _cell_out(key.accepted_aliases if key else None),
                (key.rationale or "") if key else "",
            ]
        )
    filename = f"section-{section.order_index}-questions.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class CsvImport(BaseModel):
    csv: str = Field(min_length=1, max_length=MAX_CSV_CHARS)
    mode: Literal["append", "replace"] = "append"


def _parse_csv_rows(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in ("type", "prompt") if c not in header]
    if missing:
        return [], [f"Missing column(s): {', '.join(missing)}. Expected: {', '.join(CSV_COLUMNS)}."]

    parsed: list[dict[str, Any]] = []
    problems: list[str] = []
    for line, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "") for k, v in raw.items()}
        if not any(str(v).strip() for v in row.values()):
            continue
        if len(parsed) >= MAX_CSV_ROWS:
            problems.append(f"More than {MAX_CSV_ROWS} questions; split the file.")
            break
        where = f"Row {line}"
        qtype = row.get("type", "").strip()
        prompt = row.get("prompt", "").strip()
        if qtype not in QUESTION_TYPES:
            problems.append(f"{where}: unknown type '{qtype}'.")
            continue
        if not prompt:
            problems.append(f"{where}: prompt is empty.")
            continue
        try:
            marks = float(row.get("marks") or 1)
            if marks < 0 or marks > 100:
                raise ValueError
        except ValueError:
            problems.append(f"{where}: marks must be a number from 0 to 100.")
            continue
        try:
            order_text = (row.get("display_order") or "").strip()
            display_order = int(order_text) if order_text else None
        except ValueError:
            problems.append(f"{where}: display_order must be a whole number.")
            continue
        try:
            options = _cell_in(row.get("options"))
            answers = _cell_in(row.get("correct_answers"))
            aliases = _cell_in(row.get("accepted_aliases"))
        except json.JSONDecodeError as exc:
            problems.append(f"{where}: invalid JSON ({exc.msg}).")
            continue
        if qtype in ("multiple_choice", "true_false_not_given", "yes_no_not_given") and isinstance(answers, list) and len(answers) == 1:
            answers = answers[0] if qtype != "multiple_choice" else answers
        answers = normalize_answer(qtype, answers)
        if aliases is not None and not (isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)):
            problems.append(f"{where}: accepted_aliases must be a list of strings.")
            continue
        problem = question_problem(qtype, options, answers)
        if problem:
            problems.append(f"{where} ({qtype}): {problem}")
            continue
        if answers is None:
            problems.append(f"{where}: correct_answers is empty.")
            continue
        parsed.append(
            {
                "type": qtype,
                "prompt": prompt,
                "marks": marks,
                "display_order": display_order,
                "options": options,
                "answers": answers,
                "aliases": aliases,
                "rationale": (row.get("rationale") or "").strip() or None,
            }
        )
    if not parsed and not problems:
        problems.append("The file has no questions.")
    return parsed, problems


@router.post(
    "/admin/sections/{section_id}/questions/import-csv",
    dependencies=[Depends(per_user(admin_bulk_limiter))],
)
def import_questions_csv(
    section_id: UUID,
    body: CsvImport,
    request: Request,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """All or nothing: every row is validated with the same type-specific
    rules as the editor (FR-ADMIN-04) and nothing is written if any row has
    a problem; all problems come back at once."""

    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)
    if section.module not in ("listening", "reading"):
        raise HTTPException(status_code=422, detail="CSV questions are for Listening and Reading sections only.")

    rows, problems = _parse_csv_rows(body.csv)
    if problems:
        raise HTTPException(status_code=422, detail={"message": "The CSV has problems; nothing was imported.", "problems": problems})

    existing = session.exec(select(Question).where(Question.section_id == section.id)).all()
    if body.mode == "replace":
        for question in existing:
            key = session.exec(select(AnswerKey).where(AnswerKey.question_id == question.id)).first()
            if key is not None:
                session.delete(key)
            session.delete(question)
        session.flush()
        next_order = 1
    else:
        next_order = max((q.display_order for q in existing), default=0) + 1

    now = datetime.now(timezone.utc)
    for row in rows:
        order = row["display_order"] if row["display_order"] is not None else next_order
        next_order = max(next_order, order) + 1
        question = Question(
            id=uuid4(),
            section_id=section.id,
            type=row["type"],
            prompt=row["prompt"],
            options=with_word_limit(row["type"], row["options"], section.instructions),
            display_order=order,
            marks=row["marks"],
            created_at=now,
        )
        session.add(question)
        session.flush()
        session.add(
            AnswerKey(
                id=uuid4(),
                question_id=question.id,
                correct_answers=row["answers"],
                accepted_aliases=row["aliases"],
                rationale=row["rationale"],
                created_at=now,
            )
        )
    session.commit()
    audit_note(request, mode=body.mode, imported=len(rows), replaced=len(existing) if body.mode == "replace" else 0)
    return {"imported": len(rows), "mode": body.mode}


# --- analytics -----------------------------------------------------------


def _answer_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().lower()[:80] or "(blank)"
    return json.dumps(value, sort_keys=True, ensure_ascii=False)[:80]


@router.get("/admin/tests/{test_id}/analytics")
def test_analytics(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Question analytics (FR-ADMIN-08) across every version of a test.
    Counts finished attempts only (submitted or expired). Writing and
    Speaking are never scored, so only attempt counts are shown for them."""

    test = session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found.")
    versions = {v.id: v for v in session.exec(select(TestVersion).where(TestVersion.test_id == test_id)).all()}
    attempts = (
        session.exec(
            select(Attempt).where(Attempt.test_version_id.in_(list(versions))).order_by(Attempt.created_at.desc()).limit(5000)
        ).all()
        if versions
        else []
    )
    finished = [a for a in attempts if a.status in FINISHED]
    by_version_finished: Counter = Counter(a.test_version_id for a in finished)

    scores = [a.raw_score / a.max_score for a in finished if a.raw_score is not None and a.max_score]
    bands = [a.estimated_band for a in finished if a.estimated_band is not None]
    minutes = [
        (a.submitted_at - a.started_at).total_seconds() / 60
        for a in finished
        if a.submitted_at and a.started_at and a.submitted_at >= a.started_at
    ]
    summary = {
        "attempts_started": len(attempts),
        "attempts_finished": len(finished),
        "completion_rate": round(len(finished) / len(attempts), 3) if attempts else None,
        "practice_attempts": sum(1 for a in attempts if a.mode == "practice"),
        "exam_attempts": sum(1 for a in attempts if a.mode == "exam"),
        "average_score_pct": round(100 * sum(scores) / len(scores), 1) if scores else None,
        "average_band": round(sum(bands) / len(bands), 1) if bands else None,
        "average_minutes": round(sum(minutes) / len(minutes), 1) if minutes else None,
    }

    questions_out: list[dict[str, Any]] = []
    if test.module in ("listening", "reading") and versions:
        rows = session.exec(
            select(Question, AnswerKey, Section.test_version_id, Section.order_index)
            .join(Section, Section.id == Question.section_id)
            .join(AnswerKey, AnswerKey.question_id == Question.id, isouter=True)
            .where(Section.test_version_id.in_(list(versions)))
            .order_by(Section.order_index, Question.display_order)
        ).all()
        finished_ids = [a.id for a in finished]
        responses: dict[UUID, list[Any]] = defaultdict(list)
        if finished_ids:
            for chunk_start in range(0, len(finished_ids), 500):
                chunk = finished_ids[chunk_start : chunk_start + 500]
                for resp in session.exec(select(ResponseRow).where(ResponseRow.attempt_id.in_(chunk))).all():
                    responses[resp.question_id].append(resp.response)
        for question, key, version_id, section_order in rows:
            answers = responses.get(question.id, [])
            base = by_version_finished.get(version_id, 0)
            correct = sum(1 for a in answers if key and is_correct(a, key.correct_answers, key.accepted_aliases, question.options))
            wrong = Counter(
                _answer_text(a) for a in answers if not (key and is_correct(a, key.correct_answers, key.accepted_aliases, question.options))
            )
            questions_out.append(
                {
                    "question_id": str(question.id),
                    "version_number": versions[version_id].version_number,
                    "section_order": section_order,
                    "display_order": question.display_order,
                    "type": question.type,
                    "prompt": question.prompt[:160],
                    "finished_attempts": base,
                    "answered": len(answers),
                    "correct": correct,
                    "skipped": max(base - len(answers), 0),
                    "correct_rate": round(correct / base, 3) if base else None,
                    "common_wrong_answers": [{"answer": text, "count": n} for text, n in wrong.most_common(3)],
                }
            )

    return {
        "test": {"id": str(test.id), "title": test.title, "module": test.module, "status": test.status},
        "scored": test.module in ("listening", "reading"),
        "summary": summary,
        "questions": questions_out,
    }


@router.get("/admin/analytics")
def platform_analytics(
    weeks: int = Query(default=8, ge=1, le=52),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Activity over the last N weeks, by module: started/finished attempts
    and average estimated band (Listening/Reading only), plus the busiest
    tests. Aggregates only - no student answers or names."""

    since = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    rows = session.exec(
        select(Attempt, Test.module, Test.id, Test.title)
        .join(TestVersion, TestVersion.id == Attempt.test_version_id)
        .join(Test, Test.id == TestVersion.test_id)
        .where(Attempt.created_at >= since)
    ).all()

    modules: dict[str, dict[str, Any]] = {
        m: {"started": 0, "finished": 0, "bands": []} for m in ("listening", "reading", "writing", "speaking")
    }
    weekly: dict[str, Counter] = defaultdict(Counter)
    per_test: Counter = Counter()
    titles: dict[str, str] = {}
    students: set[UUID] = set()
    for attempt, module, tid, title in rows:
        stats = modules.setdefault(module, {"started": 0, "finished": 0, "bands": []})
        stats["started"] += 1
        students.add(attempt.student_id)
        created = attempt.created_at if attempt.created_at.tzinfo else attempt.created_at.replace(tzinfo=timezone.utc)
        week = (created - timedelta(days=created.weekday())).date().isoformat()
        weekly[week][module] += 1
        per_test[str(tid)] += 1
        titles[str(tid)] = title
        if attempt.status in FINISHED:
            stats["finished"] += 1
            if attempt.estimated_band is not None:
                stats["bands"].append(attempt.estimated_band)

    by_module = {
        module: {
            "started": s["started"],
            "finished": s["finished"],
            "completion_rate": round(s["finished"] / s["started"], 3) if s["started"] else None,
            "average_band": round(sum(s["bands"]) / len(s["bands"]), 1) if s["bands"] else None,
        }
        for module, s in modules.items()
    }
    return {
        "weeks": weeks,
        "active_students": len(students),
        "attempts": len(rows),
        "by_module": by_module,
        "weekly": [{"week": week, **dict(counts)} for week, counts in sorted(weekly.items())],
        "top_tests": [{"test_id": tid, "title": titles[tid], "attempts": n} for tid, n in per_test.most_common(10)],
    }
