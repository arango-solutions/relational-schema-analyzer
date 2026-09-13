# Implementation Plan — relational-schema-analyzer

Status: Draft v0.1
Companion to [`DESIGN.md`](DESIGN.md).

This plan is **phased and low-risk**: Stage 1 is a mechanical extraction of already-tested
code from `r2g`; the new conceptual/OWL value is built in Stage 2+ where it benefits the
whole ecosystem rather than being trapped inside `r2g`.

---

## Guiding principles

- **Reverse the dependency.** `r2g` ends up depending on this library, not vice versa.
- **Baseline first.** The deterministic, no-LLM path must produce a complete, useful bundle.
  LLM is additive refinement only.
- **Contract compatibility.** Match `arango-schema-mapper`'s tool-contract v1 wire shape so a
  single consumer (e.g. `arango-ontoextract`) handles relational and Arango sources.
- **No behavior change in r2g** during extraction — verified by r2g's existing test suite.

---

## Phase 0 — Repo bootstrap (this repo)

- [x] Create repo + git
- [x] `pyproject.toml` (hatchling, name `relational-schema-analyzer`, import
      `relational_schema_analyzer`, Python ≥ 3.10)
- [x] `relational_schema_analyzer/__init__.py` public API skeleton
- [x] `.gitignore`, license (Apache-2.0), CI stub (ruff + pytest)
- [x] `docs/tool-contract/v1/response.schema.json` (+ request schema, examples) copied/adapted
      from `arango-schema-mapper/docs/tool-contract/v1/`
- [x] Create GitHub repo and push `main`

Optional extras layout (mirror Arango analyzer):
`[postgres] [mysql] [sqlserver] [snowflake] [openai] [anthropic] [openrouter] [mcp] [dev]`

---

## Phase 1 — Extract the physical core (mechanical, low risk)

Lift from `r2g` with minimal edits. **Extraction inventory** (source → destination):

| `r2g` source | New library destination | Notes |
| --- | --- | --- |
| `src/r2g/types.py` (`Schema`/`Table`/`Column`/`ForeignKey`) | `relational_schema_analyzer/types.py` (rename `Schema` → `PhysicalSchema`, alias kept) | drop `MappingConfig`/`CollectionMapping`/`EdgeDefinition` (those stay in r2g) |
| `src/r2g/connectors/base.py` | `connectors/base.py` | `SourceConnector` protocol + `create_connector` factory |
| `src/r2g/connectors/postgres.py` | `connectors/postgres.py` | incl. partition metadata rollup |
| `src/r2g/connectors/mysql.py` | `connectors/mysql.py` | |
| `src/r2g/connectors/mssql.py` | `connectors/mssql.py` | |
| `src/r2g/connectors/snowflake.py` | `connectors/snowflake.py` | |
| `src/r2g/connectors/csv_source.py` | `connectors/csv_source.py` | |
| `src/r2g/connectors/session.py` | `connectors/session.py` | bulk-read protocol (kept for r2g reuse) |
| `src/r2g/fk_inference.py` | `fk_inference.py` | name + value-overlap heuristics |
| `src/r2g/schema_diff.py` | `schema_diff.py` | reused for fingerprint/drift |
| `src/r2g/topo_sort.py` | `topo_sort.py` | |
| `src/r2g/config.py::pg_type_to_json_type`, `DEFAULT_TYPE_MAP`, `_is_likely_join_table` | `typemap.py` + `heuristics.py` | the type map + join-table heuristic only |
| relevant `tests/` (`test_*_connector.py`, `test_fk_inference.py`, `test_schema_diff.py`, CSV) | `tests/` | port as-is |

**Stays in r2g** (not extracted): `config.py` (MappingConfig generation), `config_migrate.py`,
`transformers/`, `generators/`, `connectors/arango_reader.py`, `catalog.py`, `ui/`,
`mcp_server.py`, `main.py`.

Progress (extraction landed in this pass):
- [x] `types.py` (`PhysicalSchema` + `Schema` alias; `ForeignKey`/`Column`/`Table`)
- [x] `connectors/` (`base` + `create_connector` factory, `session`, `postgres`, `mysql`,
      `mssql`, `snowflake`, `csv_source`). Kafka **dropped** for v0 (decision §9.3).
- [x] `typemap.py` (`DEFAULT_TYPE_MAP` + `pg_type_to_json_type`), `heuristics.py`
      (`is_likely_join_table`), `naming.py` (dependency-free subset), `dump_reader.py`
- [x] `schema_diff.py`, `topo_sort.py`
- [x] `fk_inference.py` — decoupled from the Arango `EdgeDefinition`: the
      `to_edge_definition()` helper is replaced by relational-native
      `InferredForeignKey.to_foreign_key() -> ForeignKey`; all four value samplers
      (Postgres/MySQL/SQL Server/CSV) + `create_value_sampler` ported
- [x] Ported tests pass (191): connectors (base/csv/mysql/mssql/snowflake), `schema_diff`,
      `topo_sort`, physical `types`, `fk_inference`; ruff clean
- [ ] `relational-schema-analyzer snapshot` CLI emits `physical.json` (CLI is Phase 3)

Exit criteria:
- [x] `create_connector(...)` builds a connector for PG/MySQL/MSSQL/Snowflake/CSV
      (URL-parsing + protocol conformance covered; live `get_schema()` needs a DB)
- [x] ported connector + fk-inference + diff tests pass
- [ ] `relational-schema-analyzer snapshot` CLI emits `physical.json`

---

## Phase 2 — Conceptual model + deterministic baseline (new value)

- [x] `conceptual.py` — `ConceptualSchema` (dict-based, shape-identical to the Arango
      analyzer for contract parity)
- [x] `mapping.py` — relational `PhysicalMapping` (`TABLE` / `FOREIGN_KEY` / `JOIN_TABLE`)
- [x] `baseline.py` — deterministic rules from DESIGN §4
  - [x] table → entity (PascalCase, matching Arango); column → datatype property
  - [x] FK → relationship (`1:1` when FK == local PK, else `1:N`)
  - [x] join table (2 FKs whose columns form the PK) → N:M relationship + association
        properties from the non-structural columns
  - [x] shared-PK-FK → `subClassOf` candidate (review-flagged)
  - [x] no declared FKs anywhere → name-based `fk_inference` fallback (relationships
        marked `inferred` + review-flagged)
- [x] `metadata.py` — confidence scoring, `reviewRequired`, `physicalSchemaFingerprint`
- [x] `analyzer.py` — `RelationalSchemaAnalyzer.analyze(physical) -> Analysis` (+ `to_bundle()`)
- [x] tests: baseline rules + analyzer + **contract conformance** (bundles validated against
      `response.schema.json` `$defs/AnalysisOutput`) + determinism
- [x] **Offline golden corpus** — the r2g CSV demo (`authors`/`books`/`members`/`loans`,
      copied to `tests/fixtures/csv_demo/`). Runs with **no DB/Docker** (CSV connector reads
      it directly) and exercises connector → baseline → inferred-FK end to end, with a
      committed golden bundle (`tests/fixtures/csv_demo_bundle.golden.json`). 215 tests, ruff clean.
- [ ] Golden bundles for Pagila / Chinook / Northwind (the **SQL-dump** corpora) — moved to
      Phase 5: these need a live DB to introspect (we don't parse DDL), so they belong with
      the Docker integration suite, gated behind `RUN_INTEGRATION`.

Exit criteria:
- [x] `analyze` produces a valid bundle conforming to `docs/tool-contract/v1/response.schema.json`
- [x] baseline runs with **no LLM** and flags ambiguous cases (`reviewRequired` +
      `detectedPatterns`: `join_table`, `inheritance_via_shared_pk`, `inferred_foreign_keys`,
      `missing_primary_key`)

---

## Phase 3 — Exports & tool contract

- [x] `exports.py` — `export_bundle()` (tool-contract JSON; accepts `Analysis` or dict).
      SQL-native physical "views" remain a future addition.
- [x] `owl_export.py` — Turtle + JSON-LD with `phys:*` annotations (DESIGN §5): `owl:Class`
      per entity (+ `rdfs:subClassOf`), `owl:DatatypeProperty` per column (PK/unique →
      `owl:FunctionalProperty` + `owl:InverseFunctionalProperty`), `owl:ObjectProperty` per
      relationship (domain/range, functional/inverse from cardinality), `phys:*` back-links.
      Default IRIs keep the `arangodb.com` host (physical ns identical to the Arango
      analyzer); both overridable.
- [x] `cli.py` — `snapshot` / `analyze` / `owl` subcommands; live source (`--source`/`--url`)
      or offline `--from-snapshot`; `-o`/stdout, `--pretty`, `--format`, `--iri-base`/
      `--phys-iri-base`.
- [x] validate emitted bundles against the JSON Schema in CI (contract-conformance tests run
      under pytest; CI installs `[dev,postgres,csv,owl]`).

Exit criteria:
- [x] `owl --format turtle` produces a `.ttl` that parses as valid RDF (rdflib round-trip in
      tests) using the same `phys:` namespace the ArangoDB analyzer publishes → importable by
      `arango-ontoextract` (full cross-repo ingestion test is Phase 5)
- [x] round-trip: OWL `phys:*` annotations resolve back to source tables/columns/FKs
      (asserted via rdflib triple queries)

---

## Phase 3.5 — Physical-model enrichment for the AOE contract

Triggered by `arango-ontoextract` feedback (2026-06): AOE wants a **mapping-agnostic, rich
physical schema model** and will own SQL→OWL/SHACL itself (it does *not* consume our OWL).
The conceptual model + OWL stay as **optional** outputs for the other consumers.

- [x] Enrich `types.py` (all additive / back-compatible): `Column` gains
      `is_unique` / `default` / `comment` / `ordinal` + computed `type_category`;
      `Table` gains `schema_name` / `comment` / `is_view` / `unique_constraints` /
      `check_constraints` / `indexes`; `ForeignKey` gains `is_unique` (cardinality hint);
      `PhysicalSchema` gains `source` provenance. New models `CheckConstraint`, `Index`,
      `SourceProvenance`.
- [x] `typemap.normalized_type_category()` — integer/decimal/boolean/string/temporal/
      binary/uuid/json/array (raw type stays authoritative).
- [x] Baseline consumes the new signals: FK `is_unique` (or FK==PK) → `1:1`; declared
      unique columns marked `unique`/`indexed`.
- [x] CSV connector populates provenance + ordinal + PK-uniqueness, with **opt-in**
      low-cardinality enum sampling (`sample_enums`) → `CheckConstraint.enum_values`.
- [x] Export the new model types; update DESIGN (consumer boundary, §3.1, S4) + tests
      (255 total, ruff clean; CSV golden bundle regenerated).
- [x] **Snowflake** catalog introspection enriched: column default/ordinal/comment, table
      comment + view flag, unique constraints (`SHOW UNIQUE KEYS`), FK cardinality hint,
      provenance + server version (`CURRENT_VERSION()`). Validated **always-on** via
      `fakesnow` (embedded emulator) + the mock-cursor unit tests.
- [x] **Postgres** catalog introspection enriched: column default/ordinal/comment
      (`pg_description`), table comment + view flag (`pg_class`/`obj_description`), unique
      constraints, CHECK constraints (`pg_get_constraintdef`), FK cardinality hint, provenance
      + server version. Assembly validated via a scripted fake-cursor unit test; live SQL by
      the Docker workflow (Postgres capabilities widened to the full set there).
- [x] **MySQL** catalog introspection enriched: default/ordinal + inline `COLUMN_COMMENT` /
      `TABLE_COMMENT`, view flag, unique constraints, CHECK constraints (8.0.16+, best-effort),
      FK cardinality hint, provenance (`VERSION()`). Assembly validated by a scripted
      fake-cursor test; Docker harness widened to the full set.
- [x] **SQL Server** catalog introspection enriched: default/ordinal, view flag, unique
      constraints, CHECK constraints (`sys.check_constraints`), comments
      (`sys.extended_properties`, best-effort), FK cardinality hint, provenance
      (`SERVERPROPERTY('ProductVersion')`). Assembly validated by a scripted fake-cursor test;
      Docker harness widened (comments asserted in the mock, not the plain-DDL live path).

---

## Testing strategy (datasource matrix)

Tiered so each engine is tested with the cheapest thing that still exercises the *real*
connector code (see the conformance harness in `tests/_conformance.py`, run against every
available backend with capability gating):

| Tier | Engines | Mechanism | Where |
| --- | --- | --- | --- |
| Embedded (always-on) | **DuckDB** | server-less; real connector over the full capability set | `tests/test_duckdb_connector.py`, main CI |
| Offline artifact (always-on) | **dbt** | `manifest.json` fixture; tests/contracts → constraints/FKs (full set minus DEFAULTS) | `tests/test_dbt_manifest_connector.py`, main CI |
| Offline artifact (always-on) | **OSI** | `*.osi.yaml` fixture; datasets/keys/relationships → tables/constraints/FKs (no types → temporal/string) | `tests/test_osi_connector.py`, main CI |
| Embedded (always-on) | **Snowflake** | `fakesnow` (DuckDB-backed, patches the driver in-process) | `tests/test_snowflake_fakesnow.py`, main CI |
| Embedded (always-on) | all dialects incl. **Databricks** | recorded result-set + mock-cursor unit tests | `tests/test_*_connector.py`, main CI |
| Embedded (always-on) | **DuckDB sampler** | the only place sampler *SQL* is executed against real rows — value overlap + all three denormalization probes, plus connector→inference→sampler end to end | `tests/test_duckdb_sampler.py`, main CI |
| Offline corpus (always-on) | CSV | real CSV connector + golden bundle | `tests/test_golden_csv.py`, main CI |
| Live Docker (CI) | **Postgres, MySQL** | service containers + `RUN_INTEGRATION` conformance | `tests/integration/`, `integration.yml` |
| Live opt-in (DSN) | SQL Server, **Snowflake**, **Databricks** | same harness, gated by a DSN env var | `tests/integration/` (skipped without DSN) |
| Live opt-in (DSN) | **Postgres DDL event log** | executes DESIGN-ADDENDUM-bitemporal §3.1's published trigger and checks RSA reads it back: DDL dated, `ALTER` later than an earlier `CREATE`, DML producing no row | `tests/integration/test_bitemporal_postgres.py` (skipped without `RSA_PG_DSN`) |
| Live opt-in (DSN) | **Postgres + MySQL sampler SQL** | value overlap + all three denormalization probes executed against real engines — the per-dialect SQL DuckDB cannot vouch for. Both verified correct; the CSV break was isolated to its Polars code, not systemic. **SQL Server and Databricks probe SQL remains unexecuted.** | `tests/integration/test_sampler_probes.py` (per-dialect DSN; each skips independently) |

- **Snowflake** → `fakesnow` for CI (real code path, no cloud) + opt-in live via `RSA_SNOWFLAKE_DSN`.
- **Databricks** (implemented) → Unity Catalog `information_schema` introspection (three-level
  `catalog.schema.table`); GA PK/FK/UNIQUE + comments in `information_schema`. Assembly
  validated by mock-cursor tests (no in-process emulator exists); live smoke is opt-in via
  `RSA_DATABRICKS_DSN`.
- **DuckDB** (implemented) → embeddable, always-on, exercises the full capability set and
  validates the generic `information_schema` FK/PK/unique resolution the RDBMS connectors reuse.
  It is also the **only tier that executes sampler SQL against real rows**. Every other sampler
  is covered by mock cursors returning a canned number, which verifies the plumbing and not one
  character of the SQL — and that gap is precisely how two CSV denormalization probes shipped a
  `TypeError` that fired the first time they touched data. A mock-only sampler test should be
  read as "unverified SQL".

---

## Phase 4 — LLM refinement (optional, additive)

- [x] `providers/` interface (openai / anthropic / openrouter) + registry
      (`register_provider` / `list_providers` / `create_provider`), copied from the Arango
      analyzer's pattern; SDKs imported lazily behind extras.
- [x] `refine.py` — generate / validate / repair loop that **refines** (not regenerates) the
      baseline: semantic renames + embed-vs-link / n-ary / description hints, applied only to
      existing elements on safe copies (no invent/drop; rename-collision validation).
- [x] `RelationalSchemaAnalyzer(llm_provider=...)` wired: provider name or object; **graceful
      fallback** to baseline on any provider/validation error; `metadata.llm` records outcome.
- [x] Refinement provenance: touched elements flip `source` to `llm`; hints are contract-valid
      additive keys. Tested with a fake provider (apply, repair-on-collision, JSON-failure,
      end-to-end analyzer path + fallback). 309 tests, ruff clean.
- [ ] **Deferred** — denormalization detection (needs value sampling) and an `eval/` harness
      comparing baseline vs LLM-refined against golden corpora (needs a live LLM + labeled
      corpora; better with the Phase 5 Docker/live corpora).

---

## Phase 5 — MCP + ecosystem integration

- [ ] **Docker integration suite** (`RUN_INTEGRATION=1`): load the r2g SQL-dump corpora
      (Pagila / Chinook / Northwind via `docker compose` + the r2g `docker/*.sql`), introspect
      with the live connectors, and assert golden conceptual bundles — the live-DB counterpart
      to the offline CSV golden corpus added in Phase 2.
- [x] `tool.py` — `run_tool(request) -> response`: the v1 tool-contract entrypoint
      (snapshot / analyze / owl), live source or captured `input.physical`; fully testable
      without the `mcp` package.
- [x] `mcp_server.py` + `relational-schema-analyzer-mcp` entry point — FastMCP wrapper
      (stdio + sse/streamable-http, bearer-token gate `RSA_MCP_TOKEN`), generic
      `..._run`/`..._run_json` + typed `snapshot`/`analyze`/`owl` tools.
- [ ] **r2g integration PR**: add dependency, replace embedded modules with imports/shims,
      delete duplicated code, wire conceptual schema into `MappingConfig` generation
      (this realizes r2g's planned Phase 10 ontology derivation via the shared lib)
- [ ] **arango-ontoextract integration**: add relational source path that calls
      `export_owl_turtle()` + provenance, alongside the existing Arango path
- [ ] Coordinate with `arango-schema-mapper` maintainers on a **shared contract package** to
      retire `MappingBundle` duplication

---

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Contract drift vs `arango-schema-analyzer` (consumers duplicate `MappingBundle`) | Pin compatible ranges; copy the v1 JSON Schema; drive toward a shared contract package in Phase 5 |
| Extraction breaks r2g | Stage 1 is behavior-preserving; gate on r2g's existing test suite; ship re-export shims |
| Relational physical mapping mistaken as Arango-queryable | DESIGN §1 makes the boundary explicit; consumers documented per-artifact |
| `arango-ontoextract` moving to its own direct extractor | Position this lib as an *additive* TTL+provenance source, not the sole path |
| Scope creep (Kafka, DDL parsing, Oracle/SQLite) | v0 = RDBMS + CSV only; defer the rest behind connector plugins |

---

## Release history

Actual cut (GitHub releases / PyPI), which differs from the original phase-by-phase
projection — the whole core (Phases 0–5) landed together in the first release:

- **v0.1.0** — first release: physical core across 7 sources (PG / MySQL / SQL Server /
  Snowflake / DuckDB / Databricks / CSV), deterministic conceptual baseline + FK inference,
  OWL (Turtle / JSON-LD) exports, CLI, optional LLM refinement, and the tool-contract + MCP
  server.
- **v0.2.0** — additive `Column` / `Table` `extra: dict` consumer-metadata passthrough
  (serialized only when non-empty; unblocks the r2g dependency-reversal compat layer).
- **v0.3.0** — physical-model enrichment for the AOE contract + the source-scope ADR
  (DESIGN §9.3.1) and the first data-catalog source: **dbt** (`manifest.json`).
- **v0.4.0** — second data-catalog source: **OSI** (`*.osi.yaml`).
- **v0.5.0** — tagged locally, **never published**; its contents shipped in v0.6.0.
- **v0.6.0** — **R2RML** export (CLI + tool contract + MCP), `ForeignKey.enforced` (unenforced
  constraints as evidence, not proof), `DatabricksValueSampler`, class-abstraction discovery
  (type-discriminator detection + `conceptual-taxonomy` integration, emitted as
  `subClassOfProposals`), physical-mapping `schema` qualification and join-table parent columns,
  and Databricks `full_data_type` (precision/scale were being discarded).
- **v0.7.0** — the **declared-key overlay** (`overlay.py`, `--overlay FILE`): human-supplied
  PK/FK/UNIQUE merged onto a `PhysicalSchema` for sources whose catalog declares none, with the
  catalog always winning, overlay keys labelled rather than laundered, and typos failing loudly.
  Plus the **injected seams** (`samplers.py`) that make taxonomy discovery reachable — before
  them, discriminator detection ran only on declared CHECK constraints and specialization
  constraints were always `null`.

- **v0.7.1** — **FK inference targets any single-column candidate key, not only the primary
  key.** Candidate-target selection only ever proposed `table.primary_key`, so a schema with a
  surrogate PK beside the natural business key everything references (`accounts.id bigint PK`
  plus `accounts.account_id text UNIQUE`) generated one candidate, saw it correctly rejected on
  type, and inferred *nothing* — on exactly the constraint-free schemas the engine exists for.
  Unique targets rank just below PK targets, so schemas where the PK is the referent are
  unchanged. Fixes the same root cause for `r2g`, whose `fk_inference` is a re-export shim.

- **v0.7.2** — **the denormalization probes had never executed.** They exist in every
  sampler, are called by nothing in this library, and had one mock test asserting the
  methods return the fake value its own fake cursor was primed with — so two of the three
  CSV probes raised `TypeError` the first time they touched real rows. Fixed, plus real-data
  coverage: a `DuckDbValueSampler` (the factory previously returned `None` for `duckdb`, so
  value analysis silently degraded to name-only) with always-on tests, and live Postgres
  probe tests. Composite FK targets now include composite UNIQUE keys, completing the 0.7.1
  candidate-key fix. `r2g` needs this version: 0.4.0 **and** 0.7.1 both carry the broken
  probes, so a real-sampler test against either passes for the wrong reason —
  `_safe_probe` swallows the error and the sampling detectors emit nothing.

- **v0.8.0** — **bitemporal stamping** (`bitemporal.py`,
  `docs/DESIGN-ADDENDUM-bitemporal.md`): every schema records when RSA observed it
  (transaction time) and when the definition became true of the source (valid time), with a
  `valid_time_source` saying how the date was obtained. Valid time is capturable only at
  introspection, so RSA is the only component that can record it — and it records without
  storing: `schema_diff` stays pure and history remains the temporal store's job. Catalog
  dates are gated by the fingerprint, which is now explicitly structure-only, so Snowflake's
  `LAST_ALTERED` moving on DML cannot fake a schema change. Additive: unstamped schemas
  serialize byte-identically to before. Reachable from the CLI (`--prior-run`), the tool
  contract and MCP (`input.previousAnalysis` — the field the shared contract already
  declared, so RSA and `arango-schema-analyzer` converge rather than fork). Also fixes
  pre-existing request-contract drift: the published request schema described an entrypoint
  RSA does not have, so every real request was invalid against RSA's own contract.

Planned next:

- **mcp 2.0 port** — the `[mcp]` extra is pinned `<2` because mcp 2.0 removed the bundled
  `mcp.server.fastmcp` in favour of a new `MCPServer` API (FastMCP moved to its own package).
  `mcp_server.py` targets the 1.x FastMCP API. Porting it is a contained piece of work; until
  then the pin is what keeps the extra installable.
- **v0.8.0** — the **BigQuery** connector with its cost governor (see
  [`PLAN-bigquery.md`](PLAN-bigquery.md)); live Docker introspection corpus (Pagila / Chinook /
  Northwind); the downstream `r2g` and `arango-ontoextract` integration PRs; shared contract
  package.
