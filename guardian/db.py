"""Small Postgres helpers and automatic, transactional SQL migrations."""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def jsonb(value):
    return Jsonb(value, dumps=canonical)


class Database:
    def __init__(self, url=None):
        self.url = url or os.environ.get(
            "DATABASE_URL", "postgresql://guardian:guardian_local@localhost:5433/guardian"
        )

    def connect(self, *, autocommit=False):
        return psycopg.connect(
            self.url,
            row_factory=dict_row,
            autocommit=autocommit,
            connect_timeout=5,
            options="-c statement_timeout=15000 -c idle_in_transaction_session_timeout=30000",
        )

    def rows(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def one(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()

    def execute(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).rowcount

    def migrate(self, directory=None):
        """A failed migration rolls back; edited applied migrations are refused."""
        directory = Path(directory or ROOT / "migrations")
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('guardian:migrations'))")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY, checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            applied = {
                r["version"]: r["checksum"]
                for r in conn.execute("SELECT version, checksum FROM schema_migrations")
            }
            files = sorted(directory.glob("[0-9]*.sql"))
            if not files:
                raise ValueError("No migration files found")
            known = {p.name for p in files}
            if set(applied) - known:
                raise ValueError(
                    "Database has migrations missing from this checkout; update the code"
                )
            for path in files:
                source = path.read_text()
                checksum = hashlib.sha256(source.encode()).hexdigest()
                if path.name in applied:
                    if applied[path.name] != checksum:
                        raise ValueError(
                            f"Applied migration changed: {path.name}; add a new migration"
                        )
                    continue
                conn.execute(source)
                conn.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )

    @contextmanager
    def lock(self, name):
        """Session locks span short transactions, not an open network transaction."""
        with self.connect(autocommit=True) as conn:
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (name,)
            ).fetchone()["acquired"]
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))


def audit(conn, kind, *, proposal_id=None, client_id=None, actor="system", detail=None):
    conn.execute(
        """INSERT INTO audit_events(kind, proposal_id, client_id, actor, detail)
           VALUES (%s, %s, %s, %s, %s)""",
        (kind, proposal_id, client_id, actor, jsonb(detail or {})),
    )
