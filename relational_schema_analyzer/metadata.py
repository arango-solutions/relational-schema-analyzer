"""Analysis metadata: fingerprint, confidence, contract-aligned metadata block.

Field names follow the tool contract (``confidence``, ``timestamp``,
``analyzedCollectionCounts``, ``detectedPatterns``) so emitted bundles validate
against ``docs/tool-contract/v1/response.schema.json`` (success criterion S2). The
relational-specific additions (``reviewRequired``, ``physicalSchemaFingerprint``,
``assumptions``) ride alongside via the contract's open ``metadata`` object.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from .types import SCHEMA_TEMPORAL_FIELDS, TABLE_TEMPORAL_FIELDS, PhysicalSchema

GENERATOR = "relational-schema-analyzer"


def fingerprint_physical_schema(schema: PhysicalSchema) -> str:
    """Stable SHA-256 of the normalized physical schema, for drift detection.

    Uses a canonical JSON dump (sorted keys) so logically-identical schemas
    fingerprint identically regardless of table/column ordering noise.

    **Bitemporal fields are excluded** (DESIGN-ADDENDUM-bitemporal §2.3). The fingerprint
    answers "is this the same schema?", so it must be a function of structure alone. The
    addendum's safety rule depends on exactly that: a catalog timestamp is trusted as
    ``valid_from`` only when the fingerprint *changed*. If observation time were hashed in,
    the fingerprint would differ on every run, every catalog date would be trusted, and the
    Snowflake case the rule exists to catch — ``LAST_ALTERED`` moving on DML — would sail
    through. Excluding them also keeps a stamped schema fingerprinting identically to the
    unstamped one it came from.
    """
    payload = schema.model_dump_json()
    # Re-normalize via the pydantic model to ensure key ordering is canonical.
    canonical = PhysicalSchema.model_validate_json(payload).model_dump(mode="json")
    blob = _canonical_json(_without_temporal(canonical))
    return "sha256-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _without_temporal(canonical: dict[str, Any]) -> dict[str, Any]:
    """Drop every bitemporal field from a serialized schema, schema- and table-level."""
    stripped = {k: v for k, v in canonical.items() if k not in SCHEMA_TEMPORAL_FIELDS}
    tables = stripped.get("tables")
    if isinstance(tables, dict):
        stripped["tables"] = {
            name: (
                {k: v for k, v in table.items() if k not in TABLE_TEMPORAL_FIELDS}
                if isinstance(table, dict)
                else table
            )
            for name, table in tables.items()
        }
    return stripped


def _canonical_json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _score_confidence(*, review_required: bool, relationships: list[dict[str, Any]]) -> float:
    """Deterministic baseline confidence in [0, 1].

    Starts at 0.9 (a clean, fully-declared relational schema is high-confidence
    without an LLM). Inferred relationships drag the score toward their own
    confidence; review flags apply a fixed penalty.
    """
    score = 0.9
    inferred = [r for r in relationships if r.get("inferred")]
    if inferred:
        avg_inferred = sum(float(r.get("confidence", 0.5)) for r in inferred) / len(inferred)
        score = min(score, 0.5 + 0.4 * avg_inferred)
    if review_required:
        score -= 0.2
    return round(max(0.0, min(1.0, score)), 3)


def build_metadata(
    schema: PhysicalSchema,
    *,
    conceptual: dict[str, Any],
    detected_patterns: list[str],
    review_required: bool,
    assumptions: list[str],
    version: str,
    warnings: Optional[list[str]] = None,
) -> dict[str, Any]:
    entities = conceptual.get("entities", [])
    relationships = conceptual.get("relationships", [])
    confidence = _score_confidence(
        review_required=review_required, relationships=relationships
    )
    # When the schema was stamped, transaction time is the observation instant recorded on
    # it, not "now" — the two can differ by the whole analysis, and the metadata should
    # report when the source was *read*.
    observed_at = schema.transaction_time or datetime.now(timezone.utc).isoformat()
    metadata = {
        "confidence": confidence,
        "timestamp": observed_at,
        "analyzedCollectionCounts": {
            "documentCollections": len(entities),
            "edgeCollections": len(relationships),
        },
        "detectedPatterns": detected_patterns,
        "reviewRequired": review_required,
        "physicalSchemaFingerprint": fingerprint_physical_schema(schema),
        "generator": GENERATOR,
        "version": version,
        "assumptions": assumptions,
        "warnings": list(warnings or []),
    }
    # Additive (DESIGN-ADDENDUM-bitemporal §2.4); empty for an unstamped schema, so a
    # caller that never stamped emits exactly the metadata it always did.
    from .bitemporal import bitemporal_metadata

    metadata.update(bitemporal_metadata(schema))
    return metadata
