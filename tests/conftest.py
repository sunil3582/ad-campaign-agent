"""Real Postgres tests, confined to an explicitly named *_eval database."""

import os
from datetime import date

import pytest
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from guardian.db import Database
from guardian.metrics import ingest, seed
from guardian.workflow import Agent
from tests.fakes import FakeModel

DAY = date(2026, 11, 9)
DESCRIPTIONS = {}


@pytest.fixture(scope="session")
def test_database():
    load_dotenv()
    options = conninfo_to_dict(Database().url)
    options["dbname"] = "guardian_eval"
    url = os.environ.get("TEST_DATABASE_URL", make_conninfo(**options))
    if not conninfo_to_dict(url).get("dbname", "").endswith("_eval"):
        pytest.fail("Refusing destructive fixtures: TEST_DATABASE_URL must name a *_eval database")
    db = Database(url)
    db.migrate()
    return db


@pytest.fixture
def db(test_database):
    # This connection is ONLY to guardian_eval, never the application database.
    with test_database.connect() as conn:
        conn.execute("TRUNCATE clients CASCADE")
        conn.execute(
            """UPDATE mock_controls SET calls=0,rate_limit_after=NULL,remaining_failures=NULL,
               retry_after=0 WHERE id=1"""
        )
    seed(test_database)
    return test_database


@pytest.fixture
def agent(db):
    return Agent(db, FakeModel(), sleep=lambda _: None)


@pytest.fixture
def snapshot(agent, db):
    for cid in ("C1", "C2", "C3"):
        ingest(db, cid, DAY)
    return agent.snapshot("C1", DAY)


@pytest.fixture
def proposal(agent, db):
    agent.daily(DAY, execute=False)
    return db.one("SELECT * FROM proposals WHERE ad_set_id='AS-102'")


def pytest_collection_modifyitems(items):
    for item in items:
        DESCRIPTIONS[item.nodeid] = (item.obj.__doc__ or "").strip().split("\n")[0]


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_sep("=", "Assignment evals: input, expected outcome, result")
    outcomes = {}
    for category in ("passed", "failed", "error", "skipped"):
        for report in terminalreporter.stats.get(category, []):
            outcomes[report.nodeid] = "PASS" if category == "passed" else category.upper()
    for node_id, result in sorted(outcomes.items()):
        terminalreporter.write_line(f"{result} {node_id}: {DESCRIPTIONS.get(node_id, '')}")
    passed = sum(result == "PASS" for result in outcomes.values())
    total = len(outcomes)
    terminalreporter.write_line(
        f"Overall: {passed}/{total} ({passed / total * 100 if total else 0:.1f}%)"
    )
