from __future__ import annotations

from pathlib import Path

import pytest

from relational_schema_analyzer import run_tool

pytest.importorskip("polars")

_CSV_DIR = str(Path(__file__).resolve().parent / "fixtures" / "csv_demo")


def _csv_source():
    return {"type": "csv", "url": _CSV_DIR}


class TestSnapshot:
    def test_ok(self):
        resp = run_tool({"operation": "snapshot", "source": _csv_source()})
        assert resp["ok"] is True
        assert resp["operation"] == "snapshot"
        assert sorted(resp["result"]["physical"]["tables"]) == [
            "authors", "books", "loans", "members",
        ]

    def test_request_id_echoed(self):
        resp = run_tool({"operation": "snapshot", "requestId": "abc", "source": _csv_source()})
        assert resp["requestId"] == "abc"


class TestAnalyze:
    def test_ok(self):
        resp = run_tool({"operation": "analyze", "source": _csv_source()})
        assert resp["ok"] is True
        bundle = resp["result"]["analysis"]
        assert set(bundle) == {"conceptualSchema", "physicalMapping", "metadata"}
        assert {e["name"] for e in bundle["conceptualSchema"]["entities"]} == {
            "Authors", "Books", "Loans", "Members",
        }

    def test_analyze_from_captured_physical(self):
        snap = run_tool({"operation": "snapshot", "source": _csv_source()})
        physical = snap["result"]["physical"]
        resp = run_tool({"operation": "analyze", "input": {"physical": physical}})
        assert resp["ok"] is True
        assert resp["result"]["analysis"]["conceptualSchema"]["entities"]


class TestOwl:
    def test_turtle(self):
        resp = run_tool({"operation": "owl", "source": _csv_source()})
        assert resp["ok"] is True
        assert resp["result"]["owl"]["format"] == "turtle"
        assert "a owl:Class" in resp["result"]["owl"]["content"]

    def test_jsonld(self):
        resp = run_tool({"operation": "owl", "source": _csv_source(),
                         "owl": {"format": "jsonld"}})
        assert resp["ok"] is True
        assert "@graph" in resp["result"]["owl"]["content"]

    def test_iri_override(self):
        resp = run_tool({"operation": "owl", "source": _csv_source(),
                         "owl": {"format": "turtle", "iriBase": "http://ex.org/c#"}})
        assert "@prefix : <http://ex.org/c#> ." in resp["result"]["owl"]["content"]


class TestR2rml:
    def test_turtle(self):
        resp = run_tool({"operation": "r2rml", "source": _csv_source()})
        assert resp["ok"] is True
        assert resp["operation"] == "r2rml"
        assert resp["result"]["r2rml"]["format"] == "turtle"
        content = resp["result"]["r2rml"]["content"]
        assert "a rr:TriplesMap" in content
        assert "rr:logicalTable" in content

    def test_from_captured_physical(self):
        snap = run_tool({"operation": "snapshot", "source": _csv_source()})
        resp = run_tool(
            {"operation": "r2rml", "input": {"physical": snap["result"]["physical"]}}
        )
        assert resp["ok"] is True
        assert "a rr:TriplesMap" in resp["result"]["r2rml"]["content"]

    def test_iri_overrides(self):
        resp = run_tool({
            "operation": "r2rml",
            "source": _csv_source(),
            "r2rml": {
                "iriBase": "http://ex.org/c#",
                "dataIriBase": "http://ex.org/row/",
                "mappingIriBase": "http://ex.org/m#",
            },
        })
        content = resp["result"]["r2rml"]["content"]
        assert "@prefix : <http://ex.org/c#> ." in content
        assert "@prefix map: <http://ex.org/m#> ." in content
        assert "http://ex.org/row/" in content

    def test_shares_an_ontology_with_the_owl_operation(self):
        # Same iriBase in both operations must yield mapping predicates the
        # ontology actually declares — that pairing is the point of shipping both.
        rdflib = pytest.importorskip("rdflib")
        args = {"source": _csv_source()}
        owl = run_tool({"operation": "owl", **args})["result"]["owl"]["content"]
        r2rml = run_tool({"operation": "r2rml", **args})["result"]["r2rml"]["content"]
        og = rdflib.Graph().parse(data=owl, format="turtle")
        rg = rdflib.Graph().parse(data=r2rml, format="turtle")
        declared = {
            s
            for t in ("Class", "DatatypeProperty", "ObjectProperty")
            for s in og.subjects(rdflib.RDF.type, rdflib.URIRef(
                f"http://www.w3.org/2002/07/owl#{t}"))
        }
        rr = "http://www.w3.org/ns/r2rml#"
        used = set(rg.objects(None, rdflib.URIRef(rr + "class"))) | set(
            rg.objects(None, rdflib.URIRef(rr + "predicate"))
        )
        assert used and used <= declared


class TestErrors:
    def test_not_a_dict(self):
        resp = run_tool("nope")  # type: ignore[arg-type]
        assert resp["ok"] is False
        assert resp["error"]["code"] == "INVALID_REQUEST"

    def test_bad_operation(self):
        resp = run_tool({"operation": "frobnicate", "source": _csv_source()})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "INVALID_REQUEST"

    def test_missing_source(self):
        resp = run_tool({"operation": "analyze"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "INVALID_REQUEST"

    def test_bad_source_surfaces_source_error(self):
        resp = run_tool({"operation": "snapshot",
                         "source": {"type": "csv", "url": "/no/such/dir"}})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "SOURCE_ERROR"


@pytest.fixture(scope="module")
def schema():
    """RSA's published request contract."""
    import json

    return json.loads(
        (Path(__file__).resolve().parent.parent
         / "docs/tool-contract/v1/request.schema.json").read_text()
    )


class TestRequestsValidateAgainstThePublishedSchema:
    """The requests `run_tool` accepts must match the schema RSA publishes.

    They did not. `docs/tool-contract/v1/request.schema.json` was copied from the ArangoDB
    analyzer and only partly adapted: the root is `additionalProperties: false` and never
    declared `source`, a conditional demanded `connection` for analyze/snapshot, and the
    export operations required `input.analysis`, which this entrypoint does not read. So
    every real RSA request was invalid against RSA's own contract — undetected, because
    nothing validated requests and no test compared the two.

    That is the same defect class arango-schema-analyzer flagged (code and schema describing
    different things). ASA validates requests at call time and caught its own; these tests
    are the cheaper half — they cannot catch a bad *caller*, but they do stop the schema and
    the entrypoint drifting apart again.
    """

    def _errors(self, schema, request):
        import jsonschema

        return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(request)]

    @pytest.mark.parametrize("operation", ["snapshot", "analyze", "owl", "r2rml"])
    def test_a_live_source_request_is_valid(self, schema, operation):
        assert not self._errors(
            schema,
            {"contractVersion": "1", "operation": operation,
             "source": {"type": "csv", "url": "/tmp/demo"}},
        )

    @pytest.mark.parametrize("operation", ["snapshot", "analyze", "owl", "r2rml"])
    def test_a_captured_physical_request_is_valid(self, schema, operation):
        assert not self._errors(
            schema,
            {"contractVersion": "1", "operation": operation,
             "input": {"physical": {"tables": {}}}},
        )

    def test_previous_analysis_is_accepted_for_bitemporal_continuity(self, schema):
        """The field the bitemporal prior run travels in must itself be contract-legal."""
        assert not self._errors(
            schema,
            {"contractVersion": "1", "operation": "analyze",
             "source": {"type": "csv", "url": "/tmp/demo"},
             "input": {"previousAnalysis": {
                 "conceptualSchema": {"entities": [], "relationships": [], "properties": []},
                 "physicalMapping": {"entities": {}, "relationships": {}},
                 "metadata": {"confidence": 1.0, "timestamp": "t",
                              "analyzedCollectionCounts": {"documentCollections": 0,
                                                           "edgeCollections": 0},
                              "detectedPatterns": []},
             }}},
        )

    def test_an_operation_with_nothing_to_read_is_rejected(self, schema):
        """The schema must still constrain, not merely permit."""
        assert self._errors(schema, {"contractVersion": "1", "operation": "analyze"})

    def test_source_params_are_accepted(self, schema):
        assert not self._errors(
            schema,
            {"contractVersion": "1", "operation": "snapshot",
             "source": {"type": "csv", "url": "/tmp/demo", "schema": "public",
                        "params": {"delimiter": ";", "has_header": False}}},
        )
