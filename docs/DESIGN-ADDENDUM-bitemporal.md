# Design addendum — bitemporal stamping of physical schemas

**Status:** IMPLEMENTED (2026-09-12, RSA `bitemporal.py`). Companion to `arango-schema-analyzer/docs/PRD.md`
§3.13.5 (the ArangoDB-side twin of this requirement) and to
`contextual-data-fabric/docs/research/unified-ontology-mapping-architecture.md`
(§7.3, §8.1, Q-3 and sequence step 7 — the decision this addendum implements).

**Context.** The portfolio decided (CDF paper, Q-3, AK 2026-09-11) that the schema and
mapping planes kept downstream are **bitemporal**: every schema version records both
*when it was true of the source* (valid time) and *when the store learned it*
(transaction time). Valid time can only be captured at introspection — nothing
downstream can reconstruct when a table was altered — and RSA is the only component
that reads the relational catalogs. So RSA must **record** both times. It must not
**store** history: RSA stays stateless, `schema_diff.py` stays side-effect free, and the
temporal store is `arango-ontoextract`'s (CDF paper §8, claim 3).

Three findings shape the requirement:

1. **RSA already stamps transaction time and nothing else (§1).** `metadata.py` writes
   `timestamp` (now) and `physicalSchemaFingerprint`; no connector reads a catalog
   change timestamp; `SourceProvenance` carries dialect, server version, database and
   namespace only.
2. **The catalogs disagree about what "last altered" means (§3).** Snowflake's
   `LAST_ALTERED` moves on DML as well as DDL; MySQL's `CREATE_TIME` never moves on
   `ALTER`; PostgreSQL has no DDL timestamp at all. A naive "read the catalog date"
   rule would be wrong on three of RSA's seven sources. The fingerprint RSA already
   computes is what makes the rule safe.
3. **Additive only (§2).** Every new field defaults to `None`, so `r2g`'s re-import of
   `PhysicalSchema` (DESIGN §3.1's standing promise) and every tool-contract v1
   consumer are unaffected.

---

## 1. What RSA has today

| Concern | Where | Status |
|---|---|---|
| Transaction time | `metadata.build_metadata()` → `timestamp` = `datetime.now(timezone.utc)` | Present, named ambiguously |
| Schema identity | `metadata.fingerprint_physical_schema()` → `physicalSchemaFingerprint` (order-insensitive SHA-256) | Present |
| Change detection | `schema_diff.py` — structural diff of two `PhysicalSchema` objects, no clock, no store | Present |
| Valid time | — | **Absent** |
| Catalog change timestamps | `connectors/{postgres,mysql,mssql,snowflake,databricks_source,duckdb_source,csv_source,dbt_manifest,osi}.py` | **None read** |
| Predecessor linkage | — | Absent |

## 2. Requirement

### 2.1 Data model (additive; all defaults `None`)

```text
Table
  valid_from: str | None            # ISO-8601 UTC; when this table's current definition became true
  valid_time_source: str | None     # see §2.2

PhysicalSchema
  transaction_time: str | None      # ISO-8601 UTC; when RSA observed the schema (= metadata.timestamp)
  valid_from: str | None            # schema-level: max(table.valid_from) over tables that have one
  valid_time_source: str | None     # the weakest source among the tables (§2.2 ordering)
  predecessor_fingerprint: str | None  # physicalSchemaFingerprint of the prior run, when the caller supplies it
```

`valid_to` is not recorded by RSA: a version is open-ended at observation. Closing it
is the temporal store's job when a successor version arrives.

### 2.2 `valid_time_source` vocabulary, strongest to weakest

| Value | Meaning |
|---|---|
| `catalog` | A DDL-scoped timestamp read from the source's own catalog |
| `event` | An operator-installed DDL event log RSA was pointed at (§3, PostgreSQL) |
| `file` | File modification time of a file-backed source (CSV) or a manifest's own generation stamp (dbt, OSI) |
| `fingerprint-continuity` | No catalog signal, but the caller supplied a prior run whose `physicalSchemaFingerprint` equals this one, so the definition was true at least since that run: `valid_from` is carried back to the earliest run in the unbroken chain |
| `observed` | No signal at all: `valid_from = transaction_time`. An auditor must be able to see this happened |

The schema-level `valid_time_source` is the **weakest** value present among tables, so a
schema is never reported as `catalog`-dated when one of its tables is only `observed`.

### 2.3 The safety rule that uses the fingerprint

A catalog timestamp is trusted as `valid_from` **only when the fingerprint changed**
relative to the supplied prior run. If the fingerprint is unchanged, the catalog
timestamp moved for a non-DDL reason (Snowflake DML, MySQL statistics) and the prior
`valid_from` is carried forward with `fingerprint-continuity`. Without a prior run RSA
has no way to tell, records the catalog value, and says so with `catalog`.

### 2.4 Emission

- **Tool contract v1 `metadata`** gains, additively: `transactionTime` (alias of
  `timestamp`), `validTime: {from}`, `validTimeSource`, `predecessorFingerprint`.
  `timestamp` stays for existing consumers.
- **CSI v1.1** (schema owned by `arango-schema-analyzer`): the same four keys under
  `provenance`; per-table `validFrom` / `validTimeSource` travel inside the relational
  physical-mapping extension when CSI gains one (CDF paper §8.1).
- **R2RML** is unaffected; it carries no time.
- **`r2g`** is the forward CSI producer for CDF and today stamps only `generatedAt` in its
  CLI. It must pass RSA's four keys through unchanged. That is a one-line `r2g` change
  and is noted here so it is not forgotten; it is not RSA scope.

### 2.5 Non-goals

No history storage. No time-travel query API. No `valid_to`. No per-column valid time
(catalogs expose table-level DDL dates at best). `schema_diff.py` stays pure.

## 3. Per-connector capture

| Connector | Signal | Source value | Caveat the implementation must encode |
|---|---|---|---|
| `snowflake.py` | `INFORMATION_SCHEMA.TABLES.LAST_ALTERED` (and `CREATED`) | `catalog` | **`LAST_ALTERED` moves on DML as well as DDL.** Trust it only under the §2.3 fingerprint rule; otherwise carry forward |
| `databricks_source.py` | Unity Catalog `information_schema.tables.last_altered` (and `created`) | `catalog` | DDL-scoped per Databricks docs; still apply §2.3 defensively |
| `mssql.py` | `sys.objects.modify_date` (and `create_date`) | `catalog` | DDL-scoped (`ALTER` and index changes). Reliable |
| `mysql.py` | `information_schema.TABLES.CREATE_TIME` | `catalog` | **`CREATE_TIME` does not move on `ALTER`**, and `UPDATE_TIME` is DML. A table altered after creation is under-dated; the fingerprint rule cannot fix an under-date. Record `catalog` with a `warnings[]` entry naming the limitation |
| `postgres.py` | None natively. If the operator has installed a DDL event trigger writing to a history table, RSA reads it | `event`, else `observed` | Reference trigger and table shape in §3.1. RSA must never create the trigger itself (read-only introspector, DESIGN §1) |
| `duckdb_source.py` | None | `observed` | `duckdb_tables()` carries no timestamps |
| `csv_source.py` | File `mtime` | `file` | Honest for a file; meaningless if the file was copied. Say `file`, not `catalog` |
| `dbt_manifest.py`, `osi.py` | Manifest `generated_at` / document generation stamp | `file` | Dates the manifest, not the warehouse |

### 3.1 PostgreSQL reference event log (operator-installed; RSA reads only)

```sql
CREATE TABLE IF NOT EXISTS rsa_ddl_history (
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  command_tag     text        NOT NULL,
  object_type     text,
  object_identity text,
  schema_name     text
);
CREATE OR REPLACE FUNCTION rsa_log_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    INSERT INTO rsa_ddl_history(command_tag, object_type, object_identity, schema_name)
    VALUES (r.command_tag, r.object_type, r.object_identity, r.schema_name);
  END LOOP;
END $$;
CREATE EVENT TRIGGER rsa_ddl_history_trg ON ddl_command_end EXECUTE FUNCTION rsa_log_ddl();
```

RSA's Postgres connector accepts an optional `ddl_history_table` (default
`rsa_ddl_history`); when present and readable, `valid_from` for a table is the latest
`occurred_at` whose `object_identity` matches, with source `event`. The read-only role
RSA connects as needs `SELECT` on that table and nothing more.

## 4. Acceptance

- Unit: a `PhysicalSchema` built from fixtures with no prior run → every table and the
  schema are `observed`, `valid_from == transaction_time`.
- Unit: same schema with a prior run of equal fingerprint dated T0 → `valid_from == T0`,
  source `fingerprint-continuity`, `predecessor_fingerprint` set; with a *different*
  prior fingerprint → catalog value trusted (where the connector has one) and
  `predecessor_fingerprint` set.
- Unit: the schema-level source is the weakest table source.
- Phase-5 Docker suite: Snowflake `LAST_ALTERED` after a DML-only change does **not**
  move `valid_from`; SQL Server `modify_date` after `ALTER TABLE` does; PostgreSQL with
  the §3.1 trigger yields `event`, without it `observed`.
- Tool contract: `metadata` validates with and without the four new keys.

## 4.1 What implementation changed or added

Three things surfaced that the requirement did not anticipate.

**The fingerprint had to be made temporal-free, and that is load-bearing.** §2.3 trusts a
catalog timestamp only when the fingerprint changed — but `fingerprint_physical_schema`
hashes the whole serialized schema, so once temporal fields existed on the model the
fingerprint would have differed on every run. Every catalog date would then be "trusted",
and the Snowflake protection the rule exists for would never fire. The fingerprint now
excludes both schema- and table-level temporal fields, which also means a stamped schema
fingerprints identically to the unstamped one it came from. Tested directly
(`TestFingerprintIsStructureOnly`).

**Placeholder timestamps are rejected.** Running the Snowflake path against the emulator
produced `valid_from: 1970-01-01` sourced as `catalog` — the driver returns epoch zero for
"no value". A fabricated 1970 date presented as catalog-sourced is worse than no date, so
`to_iso` discards anything before 1990 and the table falls through to `observed`.

**§3.1's reference trigger executes as published.** It had never been run. It is now
exercised in `tests/integration/test_bitemporal_postgres.py`: DDL is logged and dated, an
`ALTER` dates later than an earlier `CREATE`, and an `INSERT` produces no row — the
DDL-scoping the design depends on. That test also demonstrates §2.2's weakest-source rule
on real data, because `rsa_ddl_history` predates its own trigger and so is `observed`,
which correctly pulls the schema-level source down from `event`.

Also implemented beyond the letter of §2: the MySQL caveat travels on each table as
`extra.validTimeCaveat` rather than living only in documentation, since a consumer reading
`catalog` is entitled to know the date may be early.

### 4.2 Request-contract drift found while wiring this — fixed

`run_tool` requests had **never** validated against the published
`docs/tool-contract/v1/request.schema.json`, and nothing tested that they did. The schema was
copied from the ArangoDB analyzer and only partly adapted, so it described an entrypoint RSA
does not have. Three defects, all of which made *every* real RSA request invalid against
RSA's own contract:

1. the root is `additionalProperties: false` and never declared `source`, the relational
   connection descriptor the entrypoint actually takes;
2. an `allOf` conditional required `connection` for `analyze` / `snapshot`;
3. the export operations required `input.analysis`, which this entrypoint never reads —
   RSA's `owl` / `r2rml` run from a live `source` or a captured `input.physical`.

Fixed by declaring `source` and `input.physical`, and rewriting the conditionals so every
operation requires *something to read* (`source`, `input.physical`, or the shared
`connection`) rather than naming the Arango one. `connection` is retained, so the schema
still accepts the shape it shares with `arango-schema-analyzer`.

Guarded by `tests/test_tool.py::TestRequestsValidateAgainstThePublishedSchema`, which
validates every request shape the entrypoint accepts. That is the cheaper half of the fix: it
cannot catch a malformed *caller*, only the schema and the entrypoint drifting apart again.
ASA validates requests at call time and caught its own bug that way. Doing the same here
would be stronger and is the obvious follow-up, but it changes runtime behaviour for a
shipped entrypoint — `additionalProperties: false` would start rejecting callers that pass
extra keys — so it is a deliberate decision rather than a tidy-up.

`arango-schema-analyzer` does **not** share this drift: its schema matches its entrypoint and
it validates requests (verified 2026-09-12).

## 5. Open questions

1. **MySQL under-dating.** Accept the `warnings[]` entry, or add an optional
   `performance_schema` / binlog-based source in a later increment?
2. **Databricks Delta history.** `DESCRIBE HISTORY` gives per-operation timestamps that
   would allow DDL-scoped dating for Delta tables; is the extra privilege worth it?
3. **Where the prior run comes from.** ~~Should RSA define a tiny `PriorRun` input type?~~
   **Resolved: yes.** `bitemporal.PriorRun` with `PriorRun.from_metadata()`, which accepts a
   whole prior bundle or a bare metadata block and returns `None` when there is no
   fingerprint to compare — so an unusable prior behaves exactly like no prior.

   **Exposed on every surface, not just the CLI** (`--prior-run FILE`, and
   `input.previousAnalysis` on the tool contract and MCP). The first implementation wired
   only the CLI, which would have left `fingerprint-continuity` unreachable for AOE and the
   fabric's catalog builder — the consumers that commissioned this — because they call
   `run_tool`, not the CLI. Caught by `arango-schema-analyzer`'s review, which found the
   identical gap in its own `analyze_incremental`.

   The contract field is deliberately **not** a new name: `input.previousAnalysis` is
   already declared in the shared request schema, and `input` is
   `additionalProperties: false`, so a minted `priorRun` would have been rejected by any
   validating consumer. Both analyzers should use it.
