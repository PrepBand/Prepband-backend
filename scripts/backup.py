"""Free-tier backup (spec 10.2 "simple daily backup/export strategy").

Supabase's free plan keeps its own daily backups but does not let you
download them, so this makes a copy you own:

    python -m scripts.backup                 # both kinds below
    python -m scripts.backup --content-only  # no pg_dump needed

  1. backups/<timestamp>/database.sql - a full `pg_dump` of the public
     schema (tables, data, RLS policies, triggers), if pg_dump is
     installed. Restore with psql into an EMPTY project; see
     docs/RUNBOOK.md "Restore" before ever doing that.
  2. backups/<timestamp>/content/*.json - every test as a prepband-test/v1
     document (the seed format). Re-import any of them through
     Admin > Tests > Import JSON or scripts/seed_content.py.

Read-only against the database. Uses DATABASE_URL from .env; the backups/
folder is gitignored because it contains student data.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, create_engine, select

from app.core.config import get_settings
from app.models import Test
from app.services.content_io import export_test

ROOT = Path(__file__).resolve().parent.parent / "backups"


def _pg_url(url: str) -> str:
    # pg_dump wants a plain libpq URL, not SQLAlchemy's driver suffix.
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def dump_database(url: str, out: Path) -> bool:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        print("  skip database.sql: pg_dump is not installed (install the PostgreSQL client tools).")
        return False
    target = out / "database.sql"
    result = subprocess.run(
        [pg_dump, "--schema=public", "--no-owner", "--no-privileges", "--file", str(target), _pg_url(url)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Never echo the connection string; pg_dump's stderr doesn't include it.
        print(f"  pg_dump failed: {result.stderr.strip()[:500]}")
        return False
    print(f"  ok   {target} ({target.stat().st_size // 1024} KB)")
    return True


def export_content(url: str, out: Path) -> int:
    folder = out / "content"
    folder.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, pool_pre_ping=True)
    count = 0
    with Session(engine) as session:
        for test in session.exec(select(Test).order_by(Test.created_at)).all():
            slug = re.sub(r"[^a-z0-9]+", "-", test.title.lower()).strip("-")[:60] or "test"
            doc = export_test(session, test)
            doc["_backup"] = {"test_id": str(test.id), "status": test.status}
            (folder / f"{slug}-{str(test.id)[:8]}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            count += 1
    print(f"  ok   {folder} ({count} tests)")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--content-only", action="store_true", help="skip pg_dump; export test content only")
    args = parser.parse_args()

    url = get_settings().database_url
    if not url:
        print("DATABASE_URL is not set (see .env.example).")
        return 1
    out = ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    print(f"Backing up to {out}")
    if not args.content_only:
        dump_database(url, out)
    export_content(url, out)
    print("Done. Keep this folder somewhere private: it contains student data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
