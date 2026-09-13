"""PostgreSQL valid time via the §3.1 DDL event log (opt-in).

PostgreSQL exposes no DDL timestamp anywhere in its catalogs, so the addendum publishes a
reference event trigger for operators to install and has RSA read it. That reference SQL is
a claim like any other, and this executes it: creates the trigger exactly as published,
performs DDL and DML, and checks RSA reads back what actually happened.

It is also the only place any connector's ``event`` path runs, and the strongest valid-time
signal RSA can obtain — an event row is proof of DDL, where Snowflake's ``LAST_ALTERED``
has to be second-guessed.

Run with::

    RUN_INTEGRATION=1 RSA_PG_DSN=postgresql://user:pw@localhost:5432/db \
        pytest tests/integration/test_bitemporal_postgres.py
"""

from __future__ import annotations

import os
import time

import pytest

from relational_schema_analyzer.bitemporal import EVENT, OBSERVED, stamp_bitemporal
from relational_schema_analyzer.connectors.postgres import PostgresConnector

_RUN = os.environ.get("RUN_INTEGRATION") == "1"
_DSN = os.environ.get("RSA_PG_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _RUN, reason="integration tests are opt-in (set RUN_INTEGRATION=1)"),
    pytest.mark.skipif(not _DSN, reason="RSA_PG_DSN not set"),
]

SCHEMA = "rsa_bitemporal_it"

# Verbatim from DESIGN-ADDENDUM-bitemporal §3.1 — if the published SQL is wrong, this fails.
_TRIGGER_SQL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.rsa_ddl_history (
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  command_tag     text        NOT NULL,
  object_type     text,
  object_identity text,
  schema_name     text
);
CREATE OR REPLACE FUNCTION {SCHEMA}.rsa_log_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    INSERT INTO {SCHEMA}.rsa_ddl_history(command_tag, object_type, object_identity, schema_name)
    VALUES (r.command_tag, r.object_type, r.object_identity, r.schema_name);
  END LOOP;
END $$;
DROP EVENT TRIGGER IF EXISTS rsa_bitemporal_it_trg;
CREATE EVENT TRIGGER rsa_bitemporal_it_trg ON ddl_command_end
  EXECUTE FUNCTION {SCHEMA}.rsa_log_ddl();
"""


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.users (id INT PRIMARY KEY, email TEXT)")
    yield psycopg
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute("DROP EVENT TRIGGER IF EXISTS rsa_bitemporal_it_trg")
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _schema(history: str | None = "rsa_ddl_history"):
    table = f"{SCHEMA}.{history}" if history else None
    return stamp_bitemporal(
        PostgresConnector(_DSN, schema_name=SCHEMA, ddl_history_table=table).get_schema()
    )


class TestWithoutTheEventLog:
    def test_no_signal_is_reported_as_observed(self, pg):
        """The common case. Not a degradation — PostgreSQL genuinely cannot say."""
        schema = _schema(history=None)
        assert schema.tables["users"].valid_time_source == OBSERVED
        assert schema.valid_time_source == OBSERVED

    def test_a_missing_history_table_degrades_rather_than_failing(self, pg):
        """Pointing at a table that does not exist must not break introspection."""
        schema = _schema(history="nonexistent_history")
        assert schema.tables["users"].valid_time_source == OBSERVED
        assert "email" in {c.name for c in schema.tables["users"].columns}


@pytest.fixture(scope="module")
def with_trigger(pg):
    """Install §3.1's trigger, then do real DDL and one DML statement."""
    with pg.connect(_DSN, autocommit=True) as conn:
        conn.execute(_TRIGGER_SQL)
        conn.execute(f"CREATE TABLE {SCHEMA}.orders (id INT PRIMARY KEY, user_id INT)")
        time.sleep(0.05)  # so the ALTER is strictly later than the CREATE
        conn.execute(f"ALTER TABLE {SCHEMA}.users ADD COLUMN created_at timestamptz")
        conn.execute(f"INSERT INTO {SCHEMA}.users (id, email) VALUES (1, 'a@b.c')")
    return _schema()


class TestWithTheEventLog:
    def test_ddl_is_dated_from_the_event_log(self, with_trigger):
        for name in ("users", "orders"):
            table = with_trigger.tables[name]
            assert table.valid_time_source == EVENT
            assert table.valid_from

    def test_an_alter_dates_later_than_an_earlier_create(self, with_trigger):
        """The point of the log: it tracks alteration, not just creation."""
        assert with_trigger.tables["users"].valid_from > with_trigger.tables["orders"].valid_from

    def test_dml_does_not_register_as_a_schema_change(self, with_trigger, pg):
        """An INSERT ran above. A `ddl_command_end` trigger must not have logged it —
        this is precisely what Snowflake's LAST_ALTERED fails to guarantee."""
        with pg.connect(_DSN) as conn:
            tags = conn.execute(
                f"SELECT DISTINCT command_tag FROM {SCHEMA}.rsa_ddl_history"
            ).fetchall()
        assert {t[0] for t in tags} <= {"CREATE TABLE", "ALTER TABLE", "CREATE INDEX"}

    def test_schema_level_source_is_the_weakest_table(self, with_trigger):
        """`rsa_ddl_history` predates its own trigger, so it has no event row. The schema
        must not advertise itself as `event`-dated while one of its tables is `observed`."""
        sources = {t.valid_time_source for t in with_trigger.tables.values()}
        assert EVENT in sources and OBSERVED in sources
        assert with_trigger.valid_time_source == OBSERVED
