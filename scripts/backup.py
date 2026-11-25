"""Free-tier backup (spec 10.2 "simple daily backup/export strategy").

Supabase's free plan keeps its own daily backups but does not let you
download them, so this makes a copy you own:

    python -m scripts.backup                 # both kinds below
    python -m scripts.backup --content-only  # no pg_dump needed

  1. backups/<timestamp>/auth.sql and database.sql - data-only `pg_dump`s
     of the sign-in accounts (auth.users, auth.identities) and of the
     public schema, if pg_dump is installed. The schema itself comes from
     database/migrations, so a restore applies the migrations, then loads
     auth.sql (keeping every user id) and then database.sql; see
     docs/RUNBOOK.md "Full restore" before ever doing that.
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


# Data only: the schema is database/migrations, and a dump that also
# carried it would clash with the migrated schema on restore. Auth accounts
# are a separate file so a restore can load them first and keep their ids,
# which every student row references.
DUMPS = {
    "auth.sql": ["--table=auth.users", "--table=auth.identities"],
    "database.sql": ["--schema=public"],
}


def dump_database(url: str, out: Path) -> bool:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        print("  skip database dump: pg_dump is not installed (install the PostgreSQL client tools).")
        return False
    for name, selection in DUMPS.items():
        target = out / name
        result = subprocess.run(
            [pg_dump, "--data-only", *selection, "--no-owner", "--no-privileges", "--file", str(target), _pg_url(url)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Never echo the connection string; pg_dump's stderr doesn't include it.
            print(f"  pg_dump of {name} failed: {result.stderr.strip()[:500]}")
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
    dumped = True
    if not args.content_only:
        dumped = dump_database(url, out)
    # The content export still runs, so a failed dump keeps what it can.
    export_content(url, out)
    if not dumped:
        print("FAILED: the database dump didn't complete, so this backup has no student data.")
        print("Fix pg_dump and run it again, or pass --content-only to back up test content only.")
        return 1
    print("Done. Keep this folder somewhere private: it contains student data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
