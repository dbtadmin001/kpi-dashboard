"""Deciding that two sightings are the same thing.

This is the part that makes a catalog rather than nine lists side by side. A
MinIO bucket is a Terraform resource, a Kubernetes volume claim and a Trino
catalog property all at once, and nothing but this file joins them.

Three rules, tried in order, each less trustworthy than the last:

    EXPLICIT      both sides published the same native id
    DECLARED      one side's attributes name the other
    CONVENTIONAL  a configured pattern matched, e.g. s3://<bucket>/

Every alias and every edge records which rule produced it, so a wrong join is a
rule somebody can read and correct - not a mystery the graph refuses to explain.
Low confidence is surfaced, never silently dropped: an edge you can see and
distrust is worth more than one that was never drawn.
"""
import time
from typing import Iterable, List

from . import store
from .model import CONVENTIONAL, EXPLICIT, Entity, Reference, make_urn


class Resolver:
    def __init__(self, conn, run_id: int):
        self.conn = conn
        self.run_id = run_id
        self.pending = []      # (src_urn, CandidateEdge) resolved after all entities land
        self.entities = 0
        self.edges = 0
        self.unresolved = []

    # ----------------------------------------------------------------- pass 1
    def add(self, entity: Entity):
        """Record the entity and every name it is also known by."""
        urn = entity.urn
        store.upsert_resource(self.conn, entity, urn, self.run_id)
        # The entity is always an alias of itself, so an edge can target it by
        # provider-native coordinates without knowing the URN scheme.
        store.put_alias(self.conn, f"{entity.provider}::{entity.scope}/{entity.name}",
                        urn, "self", EXPLICIT, self.run_id)
        for key in entity.alias_keys():
            store.put_alias(self.conn, key, urn, "native-id", EXPLICIT, self.run_id)
        for edge in entity.edges:
            self.pending.append((urn, edge))
        self.entities += 1
        return urn

    # ----------------------------------------------------------------- pass 2
    def link(self):
        """Resolve every candidate edge once all entities exist.

        Deferred deliberately: an edge often points at something a *later*
        collector reports, and resolving eagerly would drop it.
        """
        for src, edge in self.pending:
            dst = self._target(edge.target)
            if not dst:
                self.unresolved.append({"src": src, "type": edge.type,
                                        "target": _describe(edge.target)})
                continue
            if dst == src:
                continue
            store.put_edge(self.conn, src, dst, edge.type, edge.rule,
                           edge.confidence, edge.via, self.run_id)
            self.edges += 1
        self.conn.commit()
        return self.edges

    def _target(self, ref: Reference):
        # EXPLICIT: a native id both sides publish.
        key = ref.key()
        if key:
            found = store.resolve_alias(self.conn, key)
            if found:
                return found
        # DECLARED: the edge named the target's own coordinates.
        if ref.provider and ref.name:
            found = store.resolve_alias(
                self.conn, f"{ref.provider}::{ref.scope or 'default'}/{ref.name}")
            if found:
                return found
            if ref.kind:
                urn = make_urn(ref.provider, ref.kind, ref.scope or "default", ref.name)
                if store.get_resource(self.conn, urn):
                    return urn
        return None

    def finish(self):
        self.link()
        return {"entities": self.entities, "edges": self.edges,
                "unresolved": len(self.unresolved)}


def run_collector(conn, collector, quiet=False):
    """One collection, recorded whether it works or not.

    A failure marks this provider's resources stale and keeps them. The estate
    did not disappear because we could not reach it.
    """
    run_id = store.start_run(conn, collector.name)
    started = time.time()
    try:
        entities = list(collector.collect())
    except Exception as error:                     # noqa: BLE001 - reported, not raised
        store.finish_run(conn, run_id, "failed", error=f"{type(error).__name__}: {error}")
        store.mark_stale(conn, collector.provider, run_id)
        conn.commit()
        if not quiet:
            print(f"  {collector.name:14} FAILED  {type(error).__name__}: "
                  f"{str(error).splitlines()[0][:90]}")
        return {"plugin": collector.name, "status": "failed", "entities": 0, "edges": 0}

    resolver = Resolver(conn, run_id)
    for entity in entities:
        resolver.add(entity)
    result = resolver.finish()
    store.mark_stale(conn, collector.provider, run_id)
    store.finish_run(conn, run_id, "ok", result["entities"], result["edges"])
    conn.commit()
    if not quiet:
        extra = f"  ({result['unresolved']} edges unresolved)" if result["unresolved"] else ""
        print(f"  {collector.name:14} ok      {result['entities']:4} resources, "
              f"{result['edges']:4} edges{extra}")
    return {"plugin": collector.name, "status": "ok", **result}


def relink(conn):
    """Re-resolve edges across providers after every collector has run.

    Collectors run in sequence, so an edge from the first can only point at
    something the last reported if it is retried once the estate is complete.
    """
    run_id = store.start_run(conn, "relink")
    from .collectors import all_collectors
    resolver = Resolver(conn, run_id)
    for collector in all_collectors():
        try:
            for entity in collector.collect():
                for edge in entity.edges:
                    resolver.pending.append((entity.urn, edge))
        except Exception:                          # noqa: BLE001
            continue
    edges = resolver.link()
    store.finish_run(conn, run_id, "ok", 0, edges)
    return edges


def _describe(ref: Reference) -> str:
    return ref.key() or f"{ref.provider or '?'}:{ref.kind or '?'}/{ref.name or '?'}"
