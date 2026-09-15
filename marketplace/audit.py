"""Query audit for the marketplace.

Trino's HTTP event listener POSTs one event per query. This service receives
them, keeps a durable append-only JSONL copy, and batches them into an Iceberg
table so the audit log is queryable with the same engine as everything else.

Two deliberate choices:

  * **The file write happens first.** If Trino is auditable but the audit store
    is down, we must not lose the record - the JSONL is the source of truth and
    Iceberg is a queryable projection of it. Losing an audit record is worse
    than a slow query.
  * **Batched inserts.** One INSERT per query would make the audit slower than
    the work being audited. Events buffer and flush on size or age.

    python -m marketplace.audit serve        # run the collector
    python -m marketplace.audit report       # who ran what, from Iceberg
"""
import argparse
import json
import os
import pathlib
import threading
import time
from datetime import datetime, timezone

AUDIT_SCHEMA = "marketplace_audit"
AUDIT_TABLE = f"iceberg.{AUDIT_SCHEMA}.query_log"
LOG_PATH = pathlib.Path(os.environ.get("AUDIT_LOG_PATH", "marketplace/generated/audit/query-events.jsonl"))
FLUSH_EVERY = int(os.environ.get("AUDIT_FLUSH_EVENTS", "5"))
FLUSH_SECONDS = float(os.environ.get("AUDIT_FLUSH_SECONDS", "20"))

_buffer = []
_lock = threading.Lock()
_last_flush = time.monotonic()


def connection():
    from .auth import connect_any
    return connect_any("marketplace_owner", catalog="iceberg", request_timeout=60)


def ensure_table(conn):
    cursor = conn.cursor()
    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS iceberg.{AUDIT_SCHEMA}"); cursor.fetchall()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
            query_id VARCHAR, event_time TIMESTAMP(6), username VARCHAR, principal VARCHAR,
            source VARCHAR, catalog_name VARCHAR, schema_name VARCHAR,
            query_text VARCHAR, state VARCHAR, error_code VARCHAR,
            cpu_ms BIGINT, wall_ms BIGINT, rows_returned BIGINT, bytes_scanned BIGINT,
            tables_accessed VARCHAR, audit_day VARCHAR
        ) WITH (partitioning = ARRAY['audit_day'], format = 'PARQUET')""")
    cursor.fetchall()
    # Existing tables predate the principal column. Adding it is idempotent and
    # cheap; without it every insert fails once impersonation is in use.
    try:
        cursor.execute(f"ALTER TABLE {AUDIT_TABLE} ADD COLUMN IF NOT EXISTS principal VARCHAR")
        cursor.fetchall()
    except Exception:
        pass
    cursor.close()


def _error_name(event):
    """Trino sends errorCode as an object; store its name, not its repr."""
    for candidate in (event.get("failureInfo") or {}, event):
        code = candidate.get("errorCode")
        if isinstance(code, dict):
            return code.get("name")
        if isinstance(code, str):
            return code
    return None


def flatten(event):
    """Pull the fields that matter out of Trino's QueryCompletedEvent."""
    metadata = event.get("metadata", {}) or {}
    context = event.get("context", {}) or {}
    statistics = event.get("statistics", {}) or {}
    failure = event.get("failureInfo", {}) or {}
    io = event.get("ioMetadata", {}) or {}
    inputs = io.get("inputs", []) or []
    accessed = sorted({f"{i.get('catalogName')}.{i.get('schema')}.{i.get('table')}"
                       for i in inputs if i.get("table")})
    end = event.get("endTime") or datetime.now(timezone.utc).isoformat()
    return {
        "query_id": metadata.get("queryId"),
        # Trino TIMESTAMP literals want 'YYYY-MM-DD HH:MM:SS.ffffff'; the ISO 'T'
        # separator is rejected as an invalid literal.
        "event_time": str(end)[:26].replace("Z", "").replace("T", " "),
        # `user` is who the query RAN AS; `principal` is who actually authenticated.
        # They differ only under impersonation, and that is exactly the case the
        # audit exists for - without the principal, a support engineer looking at
        # someone else's data is recorded as that someone else.
        "username": context.get("user"),
        "principal": context.get("principal"),
        "source": context.get("source"),
        "catalog_name": context.get("catalog"),
        "schema_name": context.get("schema"),
        # Trino truncates long statements itself; cap again so one pathological
        # query cannot dominate the audit table.
        "query_text": (metadata.get("query") or "")[:4000],
        "state": metadata.get("queryState") or ("FAILED" if failure else "FINISHED"),
        "error_code": _error_name(event),
        "cpu_ms": int(statistics.get("cpuTime", 0) * 1000) if isinstance(statistics.get("cpuTime"), (int, float)) else 0,
        "wall_ms": int(statistics.get("wallTime", 0) * 1000) if isinstance(statistics.get("wallTime"), (int, float)) else 0,
        "rows_returned": statistics.get("outputRows", 0) or 0,
        "bytes_scanned": statistics.get("totalBytes", 0) or 0,
        "tables_accessed": ",".join(accessed),
        "audit_day": str(end)[:10],
    }


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


COLUMNS = ["query_id", "event_time", "username", "principal", "source", "catalog_name", "schema_name",
           "query_text", "state", "error_code", "cpu_ms", "wall_ms", "rows_returned",
           "bytes_scanned", "tables_accessed", "audit_day"]


def flush(force=False):
    global _last_flush
    with _lock:
        due = force or len(_buffer) >= FLUSH_EVERY or (time.monotonic() - _last_flush) > FLUSH_SECONDS
        if not _buffer or not due:
            return 0
        batch, _buffer[:] = list(_buffer), []
        _last_flush = time.monotonic()
    rows = []
    for record in batch:
        values = []
        for column in COLUMNS:
            value = record.get(column)
            if column == "event_time":
                values.append(f"TIMESTAMP '{value}'" if value else "NULL")
            else:
                values.append(literal(value))
        rows.append("(" + ",".join(values) + ")")
    try:
        conn = connection()
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO {AUDIT_TABLE} ({','.join(COLUMNS)}) VALUES {','.join(rows)}")
        cursor.fetchall()
        cursor.close()
        conn.close()
        return len(batch)
    except Exception as error:
        # The JSONL already holds these events, so a failed flush is recoverable.
        print(f"  audit flush failed ({str(error)[:90]}); {len(batch)} events remain in the file")
        return 0


def build_app():
    from fastapi import FastAPI, Request

    app = FastAPI(title="Marketplace query audit")

    @app.get("/health")
    def health():
        with _lock:
            pending = len(_buffer)
        return {"status": "ok", "buffered": pending, "log": str(LOG_PATH)}

    @app.post("/v1/query-events")
    async def receive(request: Request):
        event = await request.json()
        record = flatten(event)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Durable first: the file is the record of truth.
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        with _lock:
            _buffer.append(record)
        flush()
        return {"accepted": record["query_id"]}

    return app


def report(limit=15):
    conn = connection()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT username, state, COUNT(*) AS queries,
               SUM(rows_returned) AS rows_returned,
               ARRAY_JOIN(ARRAY_AGG(DISTINCT tables_accessed), ' | ') AS touched
        FROM {AUDIT_TABLE}
        WHERE tables_accessed <> ''
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT {int(limit)}""")
    rows = cursor.fetchall()
    cursor.close(); conn.close()
    print(f"{'user':18} {'state':10} {'queries':>7} {'rows':>8}  tables touched")
    for username, state, queries, rows_returned, touched in rows:
        print(f"{username or '-':18} {state or '-':10} {queries:>7} {rows_returned or 0:>8}  {(touched or '')[:70]}")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Marketplace query audit")
    parser.add_argument("action", choices=["serve", "report", "init", "flush"])
    parser.add_argument("--port", type=int, default=int(os.environ.get("AUDIT_PORT", "8099")))
    args = parser.parse_args()
    if args.action == "init":
        conn = connection(); ensure_table(conn); conn.close()
        print(f"Audit table ready: {AUDIT_TABLE}")
    elif args.action == "serve":
        conn = connection(); ensure_table(conn); conn.close()
        import uvicorn
        print(f"Audit collector on 0.0.0.0:{args.port}; appending to {LOG_PATH}")
        uvicorn.run(build_app(), host="0.0.0.0", port=args.port, log_level="warning")
    elif args.action == "flush":
        print(f"Flushed {flush(force=True)} events")
    else:
        report()


if __name__ == "__main__":
    main()
