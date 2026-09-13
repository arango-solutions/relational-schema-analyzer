"""Bitemporal stamping — DESIGN-ADDENDUM-bitemporal §4 acceptance.

The interesting tests here are not "does it write a date" but the two properties the
addendum's safety rule depends on:

* the fingerprint is a function of **structure alone**, so stamping cannot change it — if
  it could, §2.3 would trust every catalog date and protect nothing;
* an unstamped schema serializes exactly as it did before the feature existed, so r2g's
  re-import promise (DESIGN §3.1) and every committed golden dump survive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relational_schema_analyzer.bitemporal import (
    CATALOG,
    EVENT,
    FILE,
    FINGERPRINT_CONTINUITY,
    OBSERVED,
    PriorRun,
    bitemporal_metadata,
    stamp_bitemporal,
    to_iso,
    weakest_source,
)
from relational_schema_analyzer.metadata import fingerprint_physical_schema
from relational_schema_analyzer.types import Column, PhysicalSchema, Table

_CSV_DIR = Path(__file__).resolve().parent / "fixtures" / "csv_demo"

T0 = "2026-01-01T00:00:00+00:00"
NOW = "2026-09-12T12:00:00+00:00"


def _table(name: str, *, valid_from=None, source=None, extra_col=None) -> Table:
    cols = [Column(name="id", data_type="integer", is_primary_key=True)]
    if extra_col:
        cols.append(Column(name=extra_col, data_type="text"))
    return Table(
        name=name,
        columns=cols,
        primary_key=["id"],
        valid_from=valid_from,
        valid_time_source=source,
    )


def _schema(*tables: Table) -> PhysicalSchema:
    return PhysicalSchema(tables={t.name: t for t in tables})


class TestBackCompatibility:
    """The feature must be invisible until it is used."""

    def test_unstamped_schema_serializes_without_the_new_keys(self):
        dumped = _schema(_table("users")).model_dump_json()
        for key in ("valid_from", "valid_time_source", "transaction_time",
                    "predecessor_fingerprint"):
            assert key not in dumped

    def test_round_trip_of_an_unstamped_schema_is_byte_identical(self):
        original = _schema(_table("users")).model_dump_json()
        assert PhysicalSchema.model_validate_json(original).model_dump_json() == original

    def test_stamping_does_not_mutate_the_input(self):
        """Callers keep the raw snapshot to show what the source actually said."""
        schema = _schema(_table("users"))
        stamp_bitemporal(schema, now=NOW)
        assert schema.transaction_time is None
        assert schema.tables["users"].valid_from is None


class TestFingerprintIsStructureOnly:
    """§2.3 rests entirely on this: the fingerprint must ignore time."""

    def test_stamping_leaves_the_fingerprint_unchanged(self):
        schema = _schema(_table("users"))
        before = fingerprint_physical_schema(schema)
        assert fingerprint_physical_schema(stamp_bitemporal(schema, now=NOW)) == before

    def test_different_observation_times_fingerprint_identically(self):
        """Otherwise every run would look like a change and no carry-forward could fire."""
        schema = _schema(_table("users"))
        a = stamp_bitemporal(schema, now="2026-01-01T00:00:00+00:00")
        b = stamp_bitemporal(schema, now="2026-09-12T12:00:00+00:00")
        assert fingerprint_physical_schema(a) == fingerprint_physical_schema(b)

    def test_a_real_structural_change_still_moves_the_fingerprint(self):
        """The exclusion must not blunt the thing the fingerprint is for."""
        a = fingerprint_physical_schema(_schema(_table("users")))
        b = fingerprint_physical_schema(_schema(_table("users", extra_col="email")))
        assert a != b

    def test_catalog_dates_from_a_connector_do_not_affect_it(self):
        plain = fingerprint_physical_schema(_schema(_table("users")))
        dated = fingerprint_physical_schema(
            _schema(_table("users", valid_from=T0, source=CATALOG))
        )
        assert plain == dated


class TestNoPriorRun:
    def test_no_signal_yields_observed_at_the_observation_time(self):
        stamped = stamp_bitemporal(_schema(_table("users")), now=NOW)
        table = stamped.tables["users"]
        assert (table.valid_from, table.valid_time_source) == (NOW, OBSERVED)
        assert (stamped.valid_from, stamped.valid_time_source) == (NOW, OBSERVED)
        assert stamped.transaction_time == NOW

    def test_a_connector_signal_is_kept_with_its_declared_source(self):
        stamped = stamp_bitemporal(
            _schema(_table("users", valid_from=T0, source=CATALOG)), now=NOW
        )
        assert stamped.tables["users"].valid_from == T0
        assert stamped.tables["users"].valid_time_source == CATALOG

    def test_no_predecessor_is_recorded_when_none_was_supplied(self):
        assert stamp_bitemporal(_schema(_table("users")), now=NOW).predecessor_fingerprint is None

    def test_an_empty_schema_is_still_dated(self):
        stamped = stamp_bitemporal(PhysicalSchema(), now=NOW)
        assert (stamped.valid_from, stamped.valid_time_source) == (NOW, OBSERVED)


class TestFingerprintContinuity:
    """§2.3 — the rule that makes an untrustworthy catalog date safe to record."""

    def test_unchanged_fingerprint_carries_the_earlier_date_back(self):
        schema = _schema(_table("users"))
        prior = PriorRun(fingerprint=fingerprint_physical_schema(schema), valid_from=T0)
        stamped = stamp_bitemporal(schema, prior=prior, now=NOW)
        assert stamped.tables["users"].valid_from == T0
        assert stamped.tables["users"].valid_time_source == FINGERPRINT_CONTINUITY
        assert stamped.predecessor_fingerprint == prior.fingerprint

    def test_a_catalog_date_is_discarded_when_the_structure_did_not_change(self):
        """The Snowflake case: LAST_ALTERED moved on DML, so it must not be believed."""
        schema = _schema(_table("users", valid_from="2026-09-12T11:00:00+00:00", source=CATALOG))
        prior = PriorRun(fingerprint=fingerprint_physical_schema(schema), valid_from=T0)
        stamped = stamp_bitemporal(schema, prior=prior, now=NOW)
        assert stamped.tables["users"].valid_from == T0
        assert stamped.tables["users"].valid_time_source == FINGERPRINT_CONTINUITY

    def test_a_changed_fingerprint_trusts_the_catalog_date(self):
        schema = _schema(_table("users", valid_from="2026-09-12T11:00:00+00:00", source=CATALOG))
        stamped = stamp_bitemporal(
            schema, prior=PriorRun(fingerprint="sha256-something-else", valid_from=T0), now=NOW
        )
        assert stamped.tables["users"].valid_from == "2026-09-12T11:00:00+00:00"
        assert stamped.tables["users"].valid_time_source == CATALOG
        assert stamped.predecessor_fingerprint == "sha256-something-else"

    def test_a_changed_fingerprint_without_a_catalog_date_falls_back_to_observed(self):
        stamped = stamp_bitemporal(
            _schema(_table("users")), prior=PriorRun(fingerprint="sha256-other", valid_from=T0),
            now=NOW,
        )
        assert stamped.tables["users"].valid_time_source == OBSERVED
        assert stamped.tables["users"].valid_from == NOW

    def test_a_prior_without_a_date_cannot_be_carried_forward(self):
        """Matching fingerprints are useless if the prior never recorded a date."""
        schema = _schema(_table("users"))
        prior = PriorRun(fingerprint=fingerprint_physical_schema(schema))
        stamped = stamp_bitemporal(schema, prior=prior, now=NOW)
        assert stamped.tables["users"].valid_time_source == OBSERVED


class TestWeakestSource:
    """§2.2 — a schema must never look better-dated than its worst table."""

    def test_ordering(self):
        assert weakest_source([CATALOG, EVENT]) == EVENT
        assert weakest_source([CATALOG, OBSERVED]) == OBSERVED
        assert weakest_source([FILE, FINGERPRINT_CONTINUITY]) == FINGERPRINT_CONTINUITY
        assert weakest_source([CATALOG]) == CATALOG
        assert weakest_source([]) is None

    def test_unknown_values_sort_weakest(self):
        """A source this build does not recognize is not something to advertise as strong."""
        assert weakest_source([CATALOG, "something-new"]) == "something-new"

    def test_schema_source_is_the_weakest_of_its_tables(self):
        stamped = stamp_bitemporal(
            _schema(
                _table("dated", valid_from=T0, source=CATALOG),
                _table("undated"),
            ),
            now=NOW,
        )
        assert stamped.tables["dated"].valid_time_source == CATALOG
        assert stamped.tables["undated"].valid_time_source == OBSERVED
        assert stamped.valid_time_source == OBSERVED

    def test_schema_valid_from_is_the_latest_table_date(self):
        stamped = stamp_bitemporal(
            _schema(
                _table("old", valid_from=T0, source=CATALOG),
                _table("new", valid_from="2026-05-05T00:00:00+00:00", source=CATALOG),
            ),
            now=NOW,
        )
        assert stamped.valid_from == "2026-05-05T00:00:00+00:00"
        assert stamped.valid_time_source == CATALOG


class TestPriorRunFromMetadata:
    def test_reads_a_whole_bundle(self):
        prior = PriorRun.from_metadata(
            {"metadata": {
                "physicalSchemaFingerprint": "sha256-abc",
                "validTime": {"from": T0},
                "validTimeSource": CATALOG,
                "transactionTime": NOW,
            }}
        )
        assert (prior.fingerprint, prior.valid_from) == ("sha256-abc", T0)
        assert prior.valid_time_source == CATALOG

    def test_reads_a_bare_metadata_block(self):
        prior = PriorRun.from_metadata(
            {"physicalSchemaFingerprint": "sha256-abc", "validTime": {"from": T0}}
        )
        assert prior.fingerprint == "sha256-abc"

    def test_falls_back_to_timestamp_for_older_bundles(self):
        """Bundles predating this feature have `timestamp` but no `transactionTime`."""
        prior = PriorRun.from_metadata(
            {"physicalSchemaFingerprint": "sha256-abc", "timestamp": NOW}
        )
        assert prior.transaction_time == NOW

    @pytest.mark.parametrize("payload", [{}, {"metadata": {}}, None, "nonsense", []])
    def test_an_unusable_prior_is_the_same_as_no_prior(self, payload):
        assert PriorRun.from_metadata(payload) is None


class TestMetadataEmission:
    """§2.4 — additive, and silent when the schema was never stamped."""

    def test_unstamped_schema_emits_nothing(self):
        assert bitemporal_metadata(_schema(_table("users"))) == {}

    def test_stamped_schema_emits_the_contract_keys(self):
        emitted = bitemporal_metadata(stamp_bitemporal(_schema(_table("users")), now=NOW))
        assert emitted["transactionTime"] == NOW
        assert emitted["validTime"] == {"from": NOW}
        assert emitted["validTimeSource"] == OBSERVED
        assert "predecessorFingerprint" not in emitted

    def test_timestamp_and_transaction_time_agree(self):
        """`timestamp` keeps its meaning; `transactionTime` is its unambiguous alias."""
        from relational_schema_analyzer import RelationalSchemaAnalyzer

        stamped = stamp_bitemporal(_schema(_table("users")), now=NOW)
        meta = RelationalSchemaAnalyzer().analyze(stamped).to_bundle()["metadata"]
        assert meta["timestamp"] == meta["transactionTime"] == NOW


class TestToIso:
    def test_naive_datetimes_are_treated_as_utc(self):
        """Guessing a local zone would silently shift catalog dates by hours."""
        from datetime import datetime, timezone

        naive = to_iso(datetime(2026, 1, 2, 3, 4))
        aware = to_iso(datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
        assert naive == aware

    def test_real_epoch_seconds_are_accepted(self):
        assert to_iso(1_757_000_000).startswith("2025-")

    def test_epoch_zero_is_rejected_as_a_placeholder(self):
        """Found by running the Snowflake path: the emulator returns 0 for LAST_ALTERED,
        which would otherwise be recorded as a `catalog`-sourced valid_from of 1970."""
        assert to_iso(0) is None
        from datetime import datetime, timezone
        assert to_iso(datetime(1970, 1, 1, tzinfo=timezone.utc)) is None

    def test_unusable_values_degrade_to_none(self):
        assert to_iso(None) is None
        assert to_iso("") is None


class TestEndToEndThroughTheCli:
    """The CLI stamps unconditionally, because only RSA can observe valid time."""

    def test_snapshot_is_stamped(self, tmp_path):
        from relational_schema_analyzer.cli import main

        out = tmp_path / "physical.json"
        assert main(["snapshot", "--source", "csv", "--url", str(_CSV_DIR), "-o", str(out)]) == 0
        dumped = json.loads(out.read_text())
        assert dumped["transaction_time"]
        # A CSV's mtime is a real signal, and it is reported as `file` — never `catalog`.
        assert dumped["valid_time_source"] == FILE
        assert all(t["valid_time_source"] == FILE for t in dumped["tables"].values())

    def test_prior_run_round_trips_from_a_previous_bundle(self, tmp_path, capsys):
        from relational_schema_analyzer.cli import main

        assert main(["analyze", "--source", "csv", "--url", str(_CSV_DIR)]) == 0
        first = json.loads(capsys.readouterr().out)
        prior_path = tmp_path / "prior.json"
        prior_path.write_text(json.dumps(first))

        assert main([
            "analyze", "--source", "csv", "--url", str(_CSV_DIR),
            "--prior-run", str(prior_path),
        ]) == 0
        second = json.loads(capsys.readouterr().out)["metadata"]
        # Same files, so the same fingerprint: the earlier date is carried forward.
        assert second["predecessorFingerprint"] == first["metadata"]["physicalSchemaFingerprint"]
        assert second["validTimeSource"] == FINGERPRINT_CONTINUITY
        assert second["validTime"]["from"] == first["metadata"]["validTime"]["from"]

    def test_an_unusable_prior_run_fails_loudly(self, tmp_path):
        from relational_schema_analyzer.cli import main

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"metadata": {"confidence": 1}}))
        with pytest.raises(SystemExit) as err:
            main(["analyze", "--source", "csv", "--url", str(_CSV_DIR), "--prior-run", str(bad)])
        assert "physicalSchemaFingerprint" in str(err.value)
