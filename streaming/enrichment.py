"""Asynchronous entity enrichment that never sits in the reporting path.

The streaming pipeline stays a pure CDC projection: it carries only what the
source system asserts. Attributes that must be fetched from a slow external
registry are resolved out of band and landed in a separate gold dimension, so:

  * a slow or failing registry can never stall a Flink checkpoint;
  * reports LEFT JOIN the dimension, so missing enrichment reads as NULL rather
    than blocking the report or inventing a value;
  * enrichment can be re-run and back-filled independently of the fact stream.

Concurrency is bounded, every call is individually timed out and retried, and a
permanent failure is recorded as a row with a status instead of being dropped.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import os
import random

TABLE = "iceberg.nda_gold.dim_entity_enrichment"
COLUMNS = ("entity_id", "legal_name", "country", "risk_tier", "registry_status",
           "enriched_at", "source_latency_ms", "status")
RISK_TIERS = ("low", "standard", "elevated", "high")
COUNTRIES = ("UG", "KE", "TZ", "RW", "ZA", "IN", "CN", "DE", "US", "GB")


def ddl():
    return (f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            " entity_id VARCHAR, legal_name VARCHAR, country VARCHAR, risk_tier VARCHAR,"
            " registry_status VARCHAR, enriched_at TIMESTAMP(6), source_latency_ms BIGINT,"
            " status VARCHAR) WITH (format='PARQUET')")


async def fetch_registry(entity_id, timeout=2.0):
    """Stand-in for the slow external registry call.

    Deterministic per entity_id so reruns agree; latency and failures are
    simulated so the retry and timeout paths are actually exercised.
    """
    digest = hashlib.sha256(entity_id.encode()).digest()
    rng = random.Random(digest)
    latency = rng.uniform(0.05, 1.2)
    await asyncio.sleep(min(latency, timeout))
    if latency > timeout:
        raise TimeoutError(f"registry call exceeded {timeout}s")
    if rng.random() < .05:
        raise ConnectionError("registry temporarily unavailable")
    return {
        "legal_name": f"Entity {digest[:3].hex().upper()} Ltd",
        "country": rng.choice(COUNTRIES),
        "risk_tier": rng.choice(RISK_TIERS),
        "registry_status": "active" if rng.random() < .9 else "suspended",
        "source_latency_ms": int(latency * 1000),
    }


async def enrich_one(entity_id, semaphore, attempts=3, timeout=2.0):
    """Bounded, retried, individually timed out. Failure yields a row, not a gap."""
    async with semaphore:
        for attempt in range(attempts):
            started = asyncio.get_running_loop().time()
            try:
                record = await asyncio.wait_for(fetch_registry(entity_id, timeout), timeout + .5)
                return dict(record, entity_id=entity_id, status="enriched",
                            enriched_at=datetime.now(timezone.utc).replace(tzinfo=None))
            except (TimeoutError, asyncio.TimeoutError, ConnectionError):
                if attempt == attempts - 1:
                    return {"entity_id": entity_id, "legal_name": None, "country": None,
                            "risk_tier": None, "registry_status": None,
                            "source_latency_ms": int((asyncio.get_running_loop().time() - started) * 1000),
                            "status": "unavailable",
                            "enriched_at": datetime.now(timezone.utc).replace(tzinfo=None)}
                await asyncio.sleep(.2 * 2 ** attempt)


async def enrich_all(entity_ids, concurrency=16):
    semaphore = asyncio.Semaphore(concurrency)
    return await asyncio.gather(*(enrich_one(e, semaphore) for e in entity_ids))


def connection():
    # Authenticated once the coordinator requires it; see marketplace/auth.py.
    from marketplace.auth import connect_any
    return connect_any(os.environ.get("TRINO_USER", "nda_dashboard"),
                       catalog="iceberg", schema="nda_gold", request_timeout=60)


def run(conn, sql):
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        cursor.close()


def pending(conn, limit):
    """Entities present in gold facts that have no enrichment row yet."""
    return [r[0] for r in run(conn, f"""
        SELECT DISTINCT a.entity_id FROM iceberg.nda_gold.all_applications a
        LEFT JOIN {TABLE} e ON e.entity_id = a.entity_id
        WHERE e.entity_id IS NULL LIMIT {int(limit)}""")]


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value:%Y-%m-%d %H:%M:%S.%f}'"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def store(conn, records):
    if not records:
        return 0
    values = ",".join("(" + ",".join(literal(r[c]) for c in COLUMNS) + ")" for r in records)
    run(conn, f"INSERT INTO {TABLE} ({','.join(COLUMNS)}) VALUES {values}")
    return len(records)


def main():
    parser = argparse.ArgumentParser(description="Asynchronously enrich entities behind the reports")
    parser.add_argument("--batch", type=int, default=200, help="Entities per pass")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--loop", action="store_true", help="Keep enriching newly arrived entities")
    parser.add_argument("--interval", type=float, default=30, help="Seconds between passes when looping")
    args = parser.parse_args()
    conn = connection()
    try:
        run(conn, ddl())
        while True:
            targets = pending(conn, args.batch)
            if targets:
                started = datetime.now()
                records = asyncio.run(enrich_all(targets, args.concurrency))
                stored = store(conn, records)
                ok = sum(r["status"] == "enriched" for r in records)
                elapsed = (datetime.now() - started).total_seconds()
                print(f"enriched {ok}/{stored} entities in {elapsed:.1f}s "
                      f"({stored/max(elapsed, .001):.0f}/s, concurrency {args.concurrency})", flush=True)
            elif not args.loop:
                print("nothing pending", flush=True)
            if not args.loop:
                break
            import time
            time.sleep(args.interval)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
