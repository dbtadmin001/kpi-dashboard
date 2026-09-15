"""Authenticated, read-only dashboard API. No fallback to the fixture on errors."""
import copy
import csv
from datetime import date, datetime, timezone
import os
import secrets
import threading
import time
import io
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from .contracts import DETAIL_LABELS, PROCESS_LABELS, RECORD_LABELS
from .serving import snapshot
from .gold_sql import kpi_query
from . import access

app = FastAPI(title="NDA regulatory streaming API", version="1.0.0")
_cache = {}
_lock = threading.Lock()


def authorize(x_api_key: str = Header(default="")):
    expected = os.environ.get("NDA_API_KEY")
    if not expected:
        raise HTTPException(503, "API authentication is not configured")
    if not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Invalid API key")


def connection():
    # The serving principal authenticates like any other caller once the
    # coordinator requires it - there is no service bypass.
    from marketplace.auth import connect_any
    return connect_any(os.environ.get("TRINO_USER", "nda_dashboard"),
                       catalog="iceberg", schema="nda_gold", request_timeout=30)


def query(conn, sql):
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        fields = [d[0] for d in cursor.description]
        return [dict(zip(fields, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def fetch_gold(since):
    conn = connection()
    try:
        facts = {family: [] for family in ("applications", "activities", "steps")}
        snapshots = {}
        measurement_sources = []
        # Each physical read is pinned to its Iceberg snapshot. Cross-table commits
        # are eventually consistent and are explicitly reported, not called atomic.
        for p in ("ma", "ct", "gmp"):
            for family in (*facts, "kpi_measurements"):
                table = f"fact_{p}_{family}"
                version = query(conn, f'SELECT snapshot_id,committed_at FROM "{table}$snapshots" ORDER BY committed_at DESC LIMIT 1')
                if not version:
                    raise HTTPException(503, f"No committed snapshot for {table}")
                version = version[0]
                snapshots[table] = {"id": version["snapshot_id"], "committed_at": str(version["committed_at"])}
                source = f"SELECT * FROM {table} FOR VERSION AS OF {int(version['snapshot_id'])} WHERE cohort_month >= '{since:%Y-%m}'"
                if family == "kpi_measurements":
                    measurement_sources.append(source)
                    continue
                rows = query(conn, source + " LIMIT 50001")
                if len(rows) > 50000:
                    raise HTTPException(413, "Reporting range exceeds the local adapter limit; narrow since or deploy SQL aggregate serving")
                facts[family].extend(rows)
        kpis = query(conn, kpi_query(" UNION ALL ".join(measurement_sources)))
        result = snapshot(**facts, kpi_rows=kpis)
        result["_meta"] = {
            "source": "sqlserver-debezium-kafka-flink-iceberg-trino", "synthetic": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "snapshots": snapshots, "consistency": "per-table snapshots; cross-table eventual consistency",
            "cohort_since": since.isoformat(), "refresh_seconds": 5,
            "limitations": ["Capacity, staffing, historic WIP and query-cycle rework facts are not yet captured; related measures are unavailable.",
                            "SLA assumptions are simulation settings pending business approval."],
        }
        return result
    finally:
        conn.close()


@app.get("/health/live")
def live():
    return {"status": "ok"}


# Deliberately unauthenticated, and deliberately returns no figures.
#
# A kubelet cannot present an API key, so an authenticated readiness probe can
# only be satisfied by putting the key in a manifest - which is worse than this
# endpoint existing. It answers exactly one question, "can this replica serve",
# by doing the same thing a request would do; the row count drives the decision
# but is not returned, so an unauthenticated caller learns nothing but up/down.
@app.get("/health/ready")
def ready():
    try:
        conn = connection()
        try:
            count = query(conn, "SELECT COUNT(*) n FROM kpi_quarterly")[0]["n"]
        finally:
            conn.close()
        if not count:
            raise HTTPException(503, "Gold has no completed KPI observations yet")
        return {"status": "ready"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Gold/Trino is unavailable") from None


@app.get("/v1/dashboard", dependencies=[Depends(authorize)])
def dashboard(since: date = Query(default=date(2025, 1, 1))):
    if since.day != 1:
        raise HTTPException(422, "since must be the first day of a month (cohort partition boundary)")
    key = since.isoformat()
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 5:
            return cached[1]
        try:
            result = fetch_gold(since)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Streaming data is unavailable; no static data was substituted") from None
        _cache.clear()
        _cache[key] = (time.monotonic(), result)
        return result


# ---------------------------------------------------------------------------
# Stakeholder serving layer.
#
# Two authentication paths coexist on purpose: the shared X-API-Key is a
# service credential for the dashboard's anonymous public view, while a bearer
# token identifies a *person* and carries the group membership every
# entitlement below is derived from.
# ---------------------------------------------------------------------------
EXPORTABLE = {
    "nda_bronze": [f"{p}_{f}" for p in ("ma", "ct", "gmp")
                   for f in ("applications", "activities", "steps")],
    "nda_silver": [f"{p}_{f}" for p in ("ma", "ct", "gmp")
                   for f in ("applications", "activities", "steps")],
    "nda_gold": [f"fact_{p}_{f}" for p in ("ma", "ct", "gmp")
                 for f in ("applications", "activities", "steps", "kpi_measurements")],
}


def describe(layer, table):
    """Turn a physical table name into something a regulator would recognise."""
    name = table[len("fact_"):] if table.startswith("fact_") else table
    process, _, record = name.partition("_")
    record_label, record_help = RECORD_LABELS.get(record, (record.replace("_", " ").title(), ""))
    detail_label, detail_help, order = DETAIL_LABELS.get(layer, (layer, "", 9))
    return {
        "id": f"{layer}|{table}", "layer": layer, "table": table,
        "record": record_label, "record_description": record_help,
        "process": PROCESS_LABELS.get(process, process.upper()),
        "process_code": process.upper(),
        "detail": detail_label, "detail_description": detail_help, "detail_order": order,
        "title": f"{PROCESS_LABELS.get(process, process.upper())} — {record_label}",
    }


def identify(authorization: str = Header(default="")):
    """Resolve a bearer token to a person and their live entitlement."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in to use this view")
    token = authorization.split(" ", 1)[1].strip()
    try:
        who = access.claims(token)
        rights = access.entitlement(who["groups"])
    except access.AuthError as error:
        raise HTTPException(401, str(error)) from None
    return {"user": who, "rights": rights}


@app.post("/v1/auth/login")
def auth_login(payload: dict):
    username, password = payload.get("username", ""), payload.get("password", "")
    if not username or not password:
        raise HTTPException(422, "Username and password are both required")
    try:
        token = access.login(username, password)
        who = access.claims(token)
        rights = access.entitlement(who["groups"])
    except access.AuthError as error:
        raise HTTPException(401, str(error)) from None
    return {"access_token": token, "user": who, "entitlement": rights}


@app.get("/v1/me")
def me(identity: dict = Depends(identify)):
    return {"user": identity["user"], "entitlement": identity["rights"]}


@app.get("/v1/catalog")
def catalog(identity: dict = Depends(identify)):
    """Only the tables this caller may actually export."""
    rights = identity["rights"]
    groups = rights.get("groups", [])
    offered, datasets = {}, []
    default_format = (rights.get("formats") or ["csv"])[0]
    for layer in EXPORTABLE:
        if layer not in rights.get("layers", []):
            continue
        allowed = [t for t in EXPORTABLE[layer]
                   if access.allows(groups, action="export", layer=layer, table=t,
                                    format=default_format)]
        if allowed:
            offered[layer] = allowed
            datasets.extend(describe(layer, table) for table in allowed)
    datasets.sort(key=lambda d: (d["detail_order"], d["process"], d["record"]))
    return {"layers": offered, "datasets": datasets, "formats": rights.get("formats", []),
            "row_limit": rights.get("row_limit", 0)}


@app.get("/v1/export")
def export(layer: str, table: str, fmt: str = Query(default="csv", alias="format"),
           since: date = Query(default=date(2025, 1, 1)),
           limit: int = Query(default=50000, ge=1),
           identity: dict = Depends(identify)):
    """Export one curated table, filtered and capped by the caller's entitlement."""
    rights, groups = identity["rights"], identity["rights"].get("groups", [])
    if layer not in EXPORTABLE or table not in EXPORTABLE[layer]:
        raise HTTPException(404, "No such table in the curated catalog")
    if not access.allows(groups, action="export", layer=layer, table=table, format=fmt):
        raise HTTPException(403, "Your access does not include this table or format")
    capped = min(limit, rights.get("row_limit", 0) or 0)
    if capped <= 0:
        raise HTTPException(403, "Your access does not include data export")
    conn = connection()
    try:
        where = "" if layer == "nda_bronze" else f" WHERE cohort_month >= '{since:%Y-%m}'"
        rows = query(conn, f"SELECT * FROM iceberg.{layer}.{table}{where} LIMIT {capped}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "The lakehouse is unavailable") from None
    finally:
        conn.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    name = f"nda-{layer}-{table}-{stamp}"
    if fmt == "csv":
        buffer = io.StringIO()
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        data = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
        media = "text/csv"
        name += ".csv"
    elif fmt == "xlsx":
        import pandas
        frame = pandas.DataFrame(rows)
        data = io.BytesIO()
        with pandas.ExcelWriter(data, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name=table[:31])
        data.seek(0)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        name += ".xlsx"
    else:
        raise HTTPException(422, "Format must be csv or xlsx")
    return StreamingResponse(data, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "X-Row-Count": str(len(rows))})


@app.get("/v1/dashboard/scoped")
def scoped_dashboard(since: date = Query(default=date(2025, 1, 1)),
                     identity: dict = Depends(identify)):
    """The dashboard contract, cut down to what this stakeholder may see."""
    if since.day != 1:
        raise HTTPException(422, "since must be the first day of a month")
    key = since.isoformat()
    with _lock:
        cached = _cache.get(key)
        payload = cached[1] if cached and time.monotonic() - cached[0] < 5 else None
    if payload is None:
        try:
            payload = fetch_gold(since)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Streaming data is unavailable") from None
        with _lock:
            _cache.clear()
            _cache[key] = (time.monotonic(), payload)
    return access.redact(copy.deepcopy(payload), identity["rights"])
