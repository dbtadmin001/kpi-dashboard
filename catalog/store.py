"""Where collected infrastructure lands.

SQLite, deliberately. The design calls for a graph database and that is right at
scale, but a first version that needs an operator to stand up Neo4j before it
shows anything is a first version nobody runs. Traversals are recursive CTEs
here; they are honest to about five hops over a few thousand nodes, which is
well past this estate. The store interface is narrow enough that swapping it is
a contained change rather than a rewrite.

Nothing here writes to any provider. The only thing this process mutates is its
own database file.
"""
import json
import os
import pathlib
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

from .taxonomy import group_for

DEFAULT_PATH = pathlib.Path(os.environ.get(
    "CATALOG_DB", pathlib.Path(__file__).resolve().parents[1] / "catalog" / "catalog.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS resource (
    urn TEXT PRIMARY KEY, provider TEXT, kind TEXT, name TEXT, scope TEXT,
    rgroup TEXT, owner TEXT, environment TEXT, status TEXT, tags TEXT,
    attributes TEXT, observed_at REAL, run_id INTEGER, stale INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS resource_kind ON resource(kind);
CREATE INDEX IF NOT EXISTS resource_provider ON resource(provider);
CREATE INDEX IF NOT EXISTS resource_scope ON resource(scope);
CREATE INDEX IF NOT EXISTS resource_group ON resource(rgroup);

-- The join, kept inspectable. Every row says which rule produced it.
CREATE TABLE IF NOT EXISTS alias (
    key TEXT PRIMARY KEY, urn TEXT, rule TEXT, confidence REAL, run_id INTEGER
);
CREATE INDEX IF NOT EXISTS alias_urn ON alias(urn);

CREATE TABLE IF NOT EXISTS edge (
    src TEXT, dst TEXT, type TEXT, rule TEXT, confidence REAL, via TEXT,
    run_id INTEGER, PRIMARY KEY (src, dst, type)
);
CREATE INDEX IF NOT EXISTS edge_src ON edge(src);
CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst);

CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, plugin TEXT, started REAL, finished REAL,
    status TEXT, entities INTEGER DEFAULT 0, edges INTEGER DEFAULT 0, error TEXT
);

-- Append-only. Who looked at what.
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, principal TEXT,
    action TEXT, target TEXT, request_id TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);

-- ---------------------------------------------------------------------
-- Delivery: which artifact is in which environment, and how it got there.
-- Separate from `resource` on purpose. A resource is something that exists;
-- an artifact is something that was built, and the two have different lives.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifact (
    key TEXT PRIMARY KEY, git_sha TEXT, images TEXT, source TEXT,
    immutable INTEGER DEFAULT 0, built_at REAL
);
CREATE TABLE IF NOT EXISTS verification (
    artifact TEXT, environment TEXT, status TEXT, detail TEXT, at REAL,
    PRIMARY KEY (artifact, environment)
);
CREATE TABLE IF NOT EXISTS promotion (
    id INTEGER PRIMARY KEY AUTOINCREMENT, artifact TEXT, from_env TEXT, to_env TEXT,
    actor TEXT, action TEXT, detail TEXT, at REAL
);
CREATE INDEX IF NOT EXISTS promotion_env ON promotion(to_env, id DESC);
CREATE TABLE IF NOT EXISTS environment (
    name TEXT PRIMARY KEY, artifact TEXT, status TEXT, since REAL
);
-- Build runs, from CI or from a local build. The control plane reads this.
CREATE TABLE IF NOT EXISTS build (
    id TEXT PRIMARY KEY, source TEXT, name TEXT, branch TEXT, sha TEXT,
    status TEXT, conclusion TEXT, started REAL, finished REAL, url TEXT, steps TEXT
);
CREATE INDEX IF NOT EXISTS build_started ON build(started DESC);
"""


def connect(path=None) -> sqlite3.Connection:
    path = pathlib.Path(path or DEFAULT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------
def start_run(conn, plugin: str) -> int:
    cursor = conn.execute(
        "INSERT INTO run (plugin, started, status) VALUES (?,?,?)",
        (plugin, time.time(), "running"))
    conn.commit()
    return cursor.lastrowid


def finish_run(conn, run_id: int, status: str, entities=0, edges=0, error=None):
    conn.execute("UPDATE run SET finished=?, status=?, entities=?, edges=?, error=? WHERE id=?",
                 (time.time(), status, entities, edges, error, run_id))
    conn.commit()


def runs(conn, limit=50) -> List[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,))]


def last_runs(conn) -> List[dict]:
    """Most recent run per plugin - what the overview shows as collector health."""
    return [dict(r) for r in conn.execute("""
        SELECT r.* FROM run r
        JOIN (SELECT plugin, MAX(id) AS id FROM run GROUP BY plugin) latest
          ON r.id = latest.id ORDER BY r.plugin""")]


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------
def upsert_resource(conn, entity, urn: str, run_id: int):
    conn.execute("""
        INSERT INTO resource (urn, provider, kind, name, scope, rgroup, owner, environment,
                              status, tags, attributes, observed_at, run_id, stale)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        ON CONFLICT(urn) DO UPDATE SET
            provider=excluded.provider, kind=excluded.kind, name=excluded.name,
            scope=excluded.scope, rgroup=excluded.rgroup,
            owner=COALESCE(excluded.owner, resource.owner),
            environment=COALESCE(excluded.environment, resource.environment),
            status=excluded.status, tags=excluded.tags,
            attributes=excluded.attributes, observed_at=excluded.observed_at,
            run_id=excluded.run_id, stale=0
    """, (urn, entity.provider, entity.kind, entity.name, entity.scope,
          group_for(entity.provider, entity.kind),
          entity.owner, entity.environment, entity.status,
          json.dumps(entity.tags), json.dumps(entity.attributes, default=str),
          time.time(), run_id))


def mark_stale(conn, provider: str, run_id: int):
    """Anything this provider used to report and did not this time.

    Marked, never deleted: a collector that cannot reach its provider must not
    be able to erase the estate. Absence of evidence is not evidence of removal.
    """
    conn.execute("UPDATE resource SET stale=1 WHERE provider=? AND run_id<>?",
                 (provider, run_id))


def put_alias(conn, key: str, urn: str, rule: str, confidence: float, run_id: int):
    conn.execute("""INSERT INTO alias (key, urn, rule, confidence, run_id) VALUES (?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET urn=excluded.urn, rule=excluded.rule,
                    confidence=excluded.confidence, run_id=excluded.run_id""",
                 (key, urn, rule, confidence, run_id))


def resolve_alias(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT urn FROM alias WHERE key=?", (key,)).fetchone()
    return row["urn"] if row else None


def put_edge(conn, src, dst, type_, rule, confidence, via, run_id):
    conn.execute("""INSERT INTO edge (src,dst,type,rule,confidence,via,run_id)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(src,dst,type) DO UPDATE SET rule=excluded.rule,
                    confidence=excluded.confidence, via=excluded.via, run_id=excluded.run_id""",
                 (src, dst, type_, rule, confidence, via, run_id))


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def _row(r) -> dict:
    d = dict(r)
    d["tags"] = json.loads(d.get("tags") or "[]")
    try:
        d["attributes"] = json.loads(d.get("attributes") or "{}")
    except (TypeError, ValueError):
        d["attributes"] = {}
    return d


def get_resource(conn, urn: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM resource WHERE urn=?", (urn,)).fetchone()
    if not row:
        return None
    item = _row(row)
    item["aliases"] = [dict(a) for a in conn.execute(
        "SELECT key, rule, confidence FROM alias WHERE urn=? ORDER BY confidence DESC", (urn,))]
    item["edges_out"] = [dict(e) for e in conn.execute(
        """SELECT e.*, r.kind AS dst_kind, r.name AS dst_name FROM edge e
           LEFT JOIN resource r ON r.urn = e.dst WHERE e.src=?""", (urn,))]
    item["edges_in"] = [dict(e) for e in conn.execute(
        """SELECT e.*, r.kind AS src_kind, r.name AS src_name FROM edge e
           LEFT JOIN resource r ON r.urn = e.src WHERE e.dst=?""", (urn,))]
    return item


def search(conn, q=None, kind=None, provider=None, scope=None, environment=None,
           owner=None, status=None, group=None, limit=200, offset=0):
    where, params = [], []
    if q:
        where.append("(name LIKE ? OR urn LIKE ? OR scope LIKE ?)")
        params += [f"%{q}%"] * 3
    for column, value in (("kind", kind), ("provider", provider), ("scope", scope),
                          ("environment", environment), ("owner", owner),
                          ("status", status), ("rgroup", group)):
        if value:
            where.append(f"{column}=?")
            params.append(value)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) n FROM resource{clause}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM resource{clause} ORDER BY provider, kind, name LIMIT ? OFFSET ?",
        params + [limit, offset])
    return {"total": total, "items": [_row(r) for r in rows],
            "facets": facets(conn, clause, params)}


def facets(conn, clause="", params=()) -> Dict[str, List[dict]]:
    """Counts alongside the results, so the UI never round-trips twice."""
    out = {}
    for column in ("rgroup", "provider", "kind", "scope", "environment", "owner", "status"):
        rows = conn.execute(
            f"SELECT {column} AS value, COUNT(*) n FROM resource{clause} "
            f"GROUP BY {column} ORDER BY n DESC", params)
        out[column] = [{"value": r["value"], "count": r["n"]} for r in rows if r["value"]]
    return out


def neighbors(conn, urn: str, depth=1, types=None) -> dict:
    """Subgraph around one node, expanded breadth-first to `depth`."""
    seen, frontier, edges = {urn}, {urn}, []
    for _ in range(max(1, min(depth, 5))):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        query = (f"SELECT * FROM edge WHERE src IN ({marks}) OR dst IN ({marks})")
        params = list(frontier) * 2
        if types:
            query += f" AND type IN ({','.join('?' * len(types))})"
            params += list(types)
        found = [dict(e) for e in conn.execute(query, params)]
        edges += found
        nxt = {e["src"] for e in found} | {e["dst"] for e in found}
        frontier = nxt - seen
        seen |= nxt
    nodes = []
    if seen:
        marks = ",".join("?" * len(seen))
        nodes = [_row(r) for r in conn.execute(
            f"SELECT * FROM resource WHERE urn IN ({marks})", list(seen))]
    unique = {(e["src"], e["dst"], e["type"]): e for e in edges}
    return {"focus": urn, "nodes": nodes, "edges": list(unique.values())}


def impact(conn, urn: str, direction="downstream", max_depth=5) -> dict:
    """Everything reachable from a node, with the distance and why.

    Downstream answers "if I change this, what is affected"; upstream answers
    "what does this need to work".
    """
    src, dst = ("src", "dst") if direction == "downstream" else ("dst", "src")
    rows = conn.execute(f"""
        WITH RECURSIVE reach(urn, hops, type, rule, confidence) AS (
            SELECT {dst}, 1, type, rule, confidence FROM edge WHERE {src} = ?
            UNION
            SELECT e.{dst}, r.hops + 1, e.type, e.rule, e.confidence
            FROM edge e JOIN reach r ON e.{src} = r.urn
            WHERE r.hops < ?
        )
        SELECT reach.urn, MIN(hops) AS hops, type, rule, MIN(confidence) AS confidence,
               res.kind, res.name, res.provider, res.scope, res.status
        FROM reach LEFT JOIN resource res ON res.urn = reach.urn
        GROUP BY reach.urn ORDER BY hops, res.kind, res.name
    """, (urn, max_depth))
    items = [dict(r) for r in rows if r["urn"] != urn]
    return {"focus": urn, "direction": direction, "count": len(items),
            "low_confidence": sum(1 for i in items if (i["confidence"] or 1) < 0.8),
            "items": items}


def stats(conn) -> dict:
    total = conn.execute("SELECT COUNT(*) n FROM resource").fetchone()["n"]
    return {
        "resources": total,
        "edges": conn.execute("SELECT COUNT(*) n FROM edge").fetchone()["n"],
        "aliases": conn.execute("SELECT COUNT(*) n FROM alias").fetchone()["n"],
        "stale": conn.execute("SELECT COUNT(*) n FROM resource WHERE stale=1").fetchone()["n"],
        "by_provider": [dict(r) for r in conn.execute(
            "SELECT provider AS value, COUNT(*) n FROM resource GROUP BY provider ORDER BY n DESC")],
        "by_kind": [dict(r) for r in conn.execute(
            "SELECT kind AS value, COUNT(*) n FROM resource GROUP BY kind ORDER BY n DESC")],
        "by_group": [dict(r) for r in conn.execute(
            "SELECT rgroup AS value, COUNT(*) n FROM resource GROUP BY rgroup ORDER BY n DESC")],
        "collectors": last_runs(conn),
    }


def record_audit(conn, principal, action, target, request_id):
    conn.execute("INSERT INTO audit (at, principal, action, target, request_id) VALUES (?,?,?,?,?)",
                 (time.time(), principal, action, target, request_id))
    conn.commit()


def audit(conn, limit=200):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
def put_artifact(conn, key, git_sha, images, source, immutable=False):
    conn.execute("""INSERT INTO artifact (key, git_sha, images, source, immutable, built_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET git_sha=excluded.git_sha,
                    images=excluded.images, immutable=excluded.immutable""",
                 (key, git_sha, json.dumps(images), source, 1 if immutable else 0, time.time()))


def get_artifact(conn, key):
    row = conn.execute("SELECT * FROM artifact WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["images"] = json.loads(item.get("images") or "{}")
    return item


def artifacts(conn, limit=25):
    out = []
    for row in conn.execute("SELECT * FROM artifact ORDER BY built_at DESC LIMIT ?", (limit,)):
        item = dict(row)
        item["images"] = json.loads(item.get("images") or "{}")
        item["verifications"] = [dict(v) for v in conn.execute(
            "SELECT environment, status, at FROM verification WHERE artifact=?", (item["key"],))]
        out.append(item)
    return out


def put_verification(conn, artifact, environment, status, detail=""):
    conn.execute("""INSERT INTO verification (artifact, environment, status, detail, at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(artifact, environment) DO UPDATE SET status=excluded.status,
                    detail=excluded.detail, at=excluded.at""",
                 (artifact, environment, status, detail, time.time()))


def get_verification(conn, artifact, environment):
    row = conn.execute("SELECT * FROM verification WHERE artifact=? AND environment=?",
                       (artifact, environment)).fetchone()
    return dict(row) if row else None


def put_promotion(conn, artifact, from_env, to_env, actor, action, detail=""):
    conn.execute("""INSERT INTO promotion (artifact, from_env, to_env, actor, action, detail, at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (artifact, from_env, to_env, actor, action, detail, time.time()))


def promotions(conn, environment=None, limit=50):
    if environment:
        rows = conn.execute("SELECT * FROM promotion WHERE to_env=? ORDER BY id DESC LIMIT ?",
                            (environment, limit))
    else:
        rows = conn.execute("SELECT * FROM promotion ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def put_environment(conn, name, artifact, status):
    conn.execute("""INSERT INTO environment (name, artifact, status, since) VALUES (?,?,?,?)
                    ON CONFLICT(name) DO UPDATE SET artifact=excluded.artifact,
                    status=excluded.status, since=excluded.since""",
                 (name, artifact, status, time.time()))


def get_environment(conn, name):
    row = conn.execute("SELECT * FROM environment WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def environments(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM environment")]


def put_build(conn, build: dict):
    conn.execute("""INSERT INTO build (id, source, name, branch, sha, status, conclusion,
                    started, finished, url, steps) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                    conclusion=excluded.conclusion, finished=excluded.finished,
                    steps=excluded.steps""",
                 (build["id"], build.get("source"), build.get("name"), build.get("branch"),
                  build.get("sha"), build.get("status"), build.get("conclusion"),
                  build.get("started"), build.get("finished"), build.get("url"),
                  json.dumps(build.get("steps") or [])))


def builds(conn, limit=40):
    out = []
    for row in conn.execute("SELECT * FROM build ORDER BY started DESC LIMIT ?", (limit,)):
        item = dict(row)
        try:
            item["steps"] = json.loads(item.get("steps") or "[]")
        except ValueError:
            item["steps"] = []
        out.append(item)
    return out
