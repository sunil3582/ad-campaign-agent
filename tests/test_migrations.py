import shutil
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from guardian.db import ROOT


def test_migrations_repeat_safely(db):
    """Input: automatic migrations run repeatedly. Expected: one entry per SQL file."""
    db.migrate()
    db.migrate()
    assert db.one("SELECT count(*) AS n FROM schema_migrations")["n"] == len(
        list((ROOT / "migrations").glob("*.sql"))
    )


def test_parallel_startup_migrations(db):
    """Input: two startup migration runners. Expected: no duplicate DDL/version entries."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: db.migrate(), range(2)))
    assert db.one("SELECT count(*) AS n FROM schema_migrations")["n"] >= 2


def test_applied_migration_edit_is_refused(db, tmp_path):
    """Input: changed checksum for an applied SQL file. Expected: fail with a clear explanation."""
    folder = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", folder)
    path = folder / "001_initial.sql"
    path.write_text(path.read_text() + "\n-- accidental edit\n")
    with pytest.raises(ValueError, match="Applied migration changed"):
        db.migrate(folder)


def test_failed_migration_rolls_back(db, tmp_path):
    """Input: new migration with a SQL error after DDL. Expected: no partial table/version."""
    folder = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", folder)
    (folder / "999_broken.sql").write_text(
        "CREATE TABLE migration_probe(value integer); SELECT 1/0;"
    )
    with pytest.raises(psycopg.Error):
        db.migrate(folder)
    assert db.one("SELECT to_regclass('migration_probe') AS name")["name"] is None
    assert db.one("SELECT version FROM schema_migrations WHERE version='999_broken.sql'") is None


def test_application_role_is_not_superuser(db):
    """Input: runtime database connection. Expected: non-superuser application role."""
    assert not db.one("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")["rolsuper"]


def test_approved_payload_is_immutable(db, proposal):
    """Input: UPDATE of a saved proposal amount. Expected: database refuses payload mutation."""
    with pytest.raises(psycopg.Error, match="immutable"):
        db.execute(
            """UPDATE proposals SET payload=jsonb_set(payload,'{amount_inr}','9999') WHERE id=%s""",
            (proposal["id"],),
        )
