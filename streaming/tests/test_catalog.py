"""The catalog reads infrastructure and never changes it.

The read-only guarantee is the product's whole premise, so it is a test rather
than a convention: if a mutating route ever appears, the build fails.
"""
import pytest

from catalog import store
from catalog.model import EDGE_TYPES, CandidateEdge, Entity, Reference, make_urn


def test_the_api_exposes_no_route_that_changes_infrastructure():
    """One POST exists and it triggers a READ of the providers."""
    from catalog.api import app
    mutating = []
    for route in app.routes:
        for method in getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}:
            mutating.append(f"{method} {route.path}")
    assert mutating == ["POST /v1/sync"], (
        f"a route that could change infrastructure appeared: {mutating}")


def test_collectors_declare_what_they_can_emit():
    from catalog.collectors import REGISTRY
    for name, factory in REGISTRY.items():
        collector = factory()
        assert collector.kinds, f"{name} declares no kinds"
        assert collector.capabilities, f"{name} declares no capabilities"
        assert hasattr(collector, "health") and hasattr(collector, "collect")


def test_every_edge_type_has_a_meaning():
    """An edge type nobody can define in one line is one the UI cannot legend."""
    for name, meaning in EDGE_TYPES.items():
        assert name.isupper() and meaning and len(meaning) < 90


def test_an_unknown_edge_type_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown edge type"):
        CandidateEdge("SORT_OF_RELATED", Reference(name="x"))


def test_urns_are_stable_and_safe():
    a = make_urn("trino", "schema", "iceberg", "nda_gold")
    assert a == "urn:infra:trino:schema:iceberg/nda_gold"
    assert a == make_urn("trino", "schema", "iceberg", "nda_gold")
    # A provider name with a space or a slash must not produce a second segment.
    assert make_urn("my provider", "kind", "sc ope", "na/me").count(":") == 4


def test_credentials_never_reach_the_catalog():
    """Terraform state is full of secrets. The catalog records that they exist."""
    from catalog.collectors.terraform import _safe
    cleaned = _safe({"name": "ok", "client_secret": "hunter2",
                     "admin_password": "s3cret", "bearer_token": "abc"})
    assert cleaned["name"] == "ok"
    for key in ("client_secret", "admin_password", "bearer_token"):
        assert cleaned[key] == "***redacted***"


def test_a_failed_collector_marks_stale_and_never_deletes(tmp_path):
    """Absence of evidence is not evidence of deletion."""
    conn = store.connect(tmp_path / "t.db")
    run = store.start_run(conn, "demo")
    store.upsert_resource(conn, Entity(provider="demo", kind="thing", name="a"),
                          "urn:infra:demo:thing:default/a", run)
    conn.commit()

    class Broken:
        name = provider = "demo"
        def collect(self):
            raise ConnectionError("provider unreachable")

    from catalog.resolver import run_collector
    result = run_collector(conn, Broken(), quiet=True)
    assert result["status"] == "failed"
    survivor = store.get_resource(conn, "urn:infra:demo:thing:default/a")
    assert survivor is not None, "a failed collection deleted the estate"
    assert survivor["stale"] == 1
    conn.close()


def test_edges_carry_the_rule_that_produced_them(tmp_path):
    """A wrong join must be a rule somebody can read, not a mystery."""
    conn = store.connect(tmp_path / "t.db")
    run = store.start_run(conn, "demo")
    from catalog.resolver import Resolver
    resolver = Resolver(conn, run)
    resolver.add(Entity(provider="minio", kind="bucket", name="warehouse",
                        native_ids={"s3-uri": "s3://warehouse/"}))
    resolver.add(Entity(provider="trino", kind="catalog", name="iceberg", edges=[
        CandidateEdge("STORES_IN", Reference(source="s3-uri", native_id="s3://warehouse/"),
                      rule="catalog-warehouse-uri", confidence=0.6)]))
    resolver.finish()
    bucket = store.get_resource(conn, "urn:infra:minio:bucket:default/warehouse")
    assert len(bucket["edges_in"]) == 1
    edge = bucket["edges_in"][0]
    assert edge["rule"] == "catalog-warehouse-uri" and edge["confidence"] == 0.6
    conn.close()
