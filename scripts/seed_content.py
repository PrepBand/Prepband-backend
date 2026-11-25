"""Load the starter content in database/seeds/*.json into the database.

    python -m scripts.seed_content --dry-run     # validate only, no DB access
    python -m scripts.seed_content               # import as drafts
    python -m scripts.seed_content --publish     # import, then publish the tests
                                                 # imported in THIS run that pass
                                                 # validation (existing tests are
                                                 # never changed)

Uses DATABASE_URL from .env - that is the LIVE Supabase database, so this
is a real write (see Docs/rules.md: ask before running it). Idempotent: a
seed whose title already exists as a test is skipped, never duplicated.

Listening seeds import with *pending* audio placeholders (transcripts
included). They can't be published until the audio is uploaded through
the admin panel (Admin > Tests > the test > Upload audio), because publish
validation requires real audio. Generate the audio first with
scripts/generate_listening_audio.ps1.

Writing and Speaking seeds load like any other (Phase 3). Re-running is
safe: titles that already exist are skipped.
"""

import argparse
import json
import sys
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Test, TestVersion
from app.services.content_io import TestDoc, import_test, validate_for_publish

SEEDS_DIR = Path(__file__).resolve().parent.parent / "database" / "seeds"


def load_docs() -> list[tuple[Path, TestDoc]]:
    docs = []
    for path in sorted(SEEDS_DIR.glob("*.json")):
        docs.append((path, TestDoc.model_validate(json.loads(path.read_text(encoding="utf-8")))))
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="validate the seed files without touching the database")
    parser.add_argument("--publish", action="store_true", help="publish every imported test that passes validation")
    args = parser.parse_args()

    docs = load_docs()
    for path, doc in docs:
        questions = sum(len(s.questions) for s in doc.sections)
        print(f"  ok  {path.name}: {doc.kind} {doc.module}/{doc.track}, {len(doc.sections)} section(s), {questions} questions")
    if args.dry_run:
        print(f"{len(docs)} seed file(s) valid. Dry run: nothing written.")
        return 0

    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    from sqlmodel import create_engine

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as session:
        for _path, doc in docs:
            existing = session.exec(select(Test).where(Test.title == doc.title)).first()
            test = existing
            if existing is None:
                test = import_test(session, doc)
                session.commit()
                session.refresh(test)
                print(f"  imported   {doc.title}  ({test.id})")
            else:
                # Never touch a test that already exists - an admin may be
                # holding it as a draft on purpose.
                print(f"  skipped    {doc.title}  (already exists: {existing.id}, {existing.status})")
                continue

            if args.publish and test.status == "draft":
                problems = validate_for_publish(session, test)
                if problems:
                    print(f"  not published: {doc.title}: {problems[0]}" + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""))
                    continue
                version = session.get(TestVersion, test.current_version_id)
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                version.status = "published"
                version.published_at = now
                test.status = "published"
                session.add(version)
                session.add(test)
                session.commit()
                print(f"  published  {doc.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
