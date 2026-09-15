"""Seeded application lifecycles written to SQL Server, never directly to Kafka."""
import argparse
from datetime import datetime, timedelta, timezone
import heapq
import os
import random
import time
from uuid import NAMESPACE_URL, uuid5
from .contracts import COLUMNS, PROCESSES, ROUTES, reference, rules, table_names


def uid(value):
    return str(uuid5(NAMESPACE_URL, value))


ARRIVAL_DAYS = 2.5
MAX_INTAKE = 10000


def intake(count, start, as_of, arrival_days=ARRIVAL_DAYS):
    """Applications admitted per process by the as_of horizon.

    Arrival cadence is fixed, so extending the horizon only appends applications
    and never reschedules the ones already received.
    """
    if arrival_days <= 0:
        raise ValueError("arrival_days must be positive")
    horizon = None
    if as_of is not None:
        horizon = max(0, int((as_of - start).total_seconds() / 86400 // arrival_days) + 1)
    if count > 0:
        return count if horizon is None else min(count, horizon)
    if horizon is None:
        raise ValueError("Unbounded intake (count=0) requires an as_of horizon")
    return min(horizon, MAX_INTAKE)


def lifecycles(count=30, seed=42, start=None, as_of=None, arrival_days=ARRIVAL_DAYS):
    """Yield globally time-ordered INSERT/UPDATE states, including in-flight work.

    Business timestamps use an explicit accelerated simulation clock. Replaying
    the same seed/start is idempotent in SQL via record_id and revision.
    An as_of cutoff stops that clock: work whose remaining states fall after it
    stays open at RECEIVED/IN_REVIEW, so the source keeps a realistic backlog
    instead of a fully drained history. Applications keep arriving up to that
    horizon, so advancing it grows the caseload rather than only draining it.
    """
    start = start or datetime(2025, 4, 1)
    data = reference()
    events = []
    sequence = 0
    def queue(table, row, when):
        nonlocal sequence
        sequence += 1
        heapq.heappush(events, (when, sequence, table, row))
    def states(table, row, completed):
        initial = dict(row, completed_at=None, status="RECEIVED", outcome=None,
                       updated_at=row["received_at"], revision=1, touch_days=0., wait_days=0.)
        queue(table, initial, row["received_at"])
        progress = dict(initial, status="IN_REVIEW", revision=2,
                        updated_at=row["received_at"] + (completed - row["received_at"]) / 3)
        queue(table, progress, progress["updated_at"])
        final = dict(row, completed_at=completed, updated_at=completed, status="COMPLETED", revision=3)
        queue(table, final, completed)
    for process in PROCESSES:
        variants = {"MA": ["new", "renewal", "variation"], "CT": ["new", "amendment"], "GMP": ["inspection"]}[process]
        for i in range(intake(count, start, as_of, arrival_days)):
            # Per-application stream: admitting later applications never redraws earlier ones.
            rng = random.Random(f"{seed}:{process}:{i}")
            received = start + timedelta(days=i * arrival_days, hours=rng.uniform(0, 6))
            app_id = uid(f"nda:{seed}:{start.isoformat()}:{process}:{i}")
            app_type = rng.choice(variants)
            route = ROUTES[i % len(ROUTES)]
            common = dict(application_id=app_id, entity_id=uid(f"entity:{process}:{i // 2}"),
                          process_code=process, application_type=app_type, route=route,
                          cohort_month=received.strftime("%Y-%m"), received_at=received,
                          due_at=received + timedelta(days=90), touch_days=0., wait_days=0.)
            relevant = [r for r in rules() if r.process == process and (not r.application_type or r.application_type == app_type) and not r.routes]
            activities = {r.activity: r for r in relevant}
            last = received
            for activity, rule in activities.items():
                ts = received if activity in ("evaluation", "decision") else received + timedelta(days=rng.uniform(1, 5))
                probability = min(1., max(0., data["quarterlyData"][process][rule.key]["data"][-1]["value"] / 100)) if rule.aggregate == "percentage" else .8
                duration = rule.sla_days * (rng.uniform(.35, .95) if rng.random() < probability else rng.uniform(1.05, 1.55))
                done = ts + timedelta(days=duration)
                last = max(last, done)
                row = dict(common, record_id=uid(f"{app_id}:{activity}:1"), activity_type=activity,
                           received_at=ts, due_at=ts + timedelta(days=rule.sla_days),
                           outcome=("COMPLIANT" if rng.random() < .82 else "NON_COMPLIANT") if activity == "inspection" else "APPROVED",
                           touch_days=duration * .6, wait_days=duration * .4)
                states(f"{process.lower()}_activities", row, done)
            steps = [(name, spec) for name, spec in data["processStepData"][process].items() if "_" not in name]
            ts = received
            for name, spec in steps:
                target = max(.1, float(spec["data"][-1]["targetDays"]))
                duration = max(.05, float(spec["data"][-1]["avgDays"])) * rng.uniform(.65, 1.35)
                done = ts + timedelta(days=duration)
                row = dict(common, record_id=uid(f"{app_id}:step:{name}:1"), activity_type=name,
                           received_at=ts, due_at=ts + timedelta(days=target), outcome="COMPLETED",
                           touch_days=duration * .65, wait_days=duration * .35)
                states(f"{process.lower()}_steps", row, done)
                ts = done
            last = max(last, ts)
            app = dict(common, record_id=app_id, activity_type="application", outcome="APPROVED",
                       touch_days=(last-received).total_seconds()/86400*.6,
                       wait_days=(last-received).total_seconds()/86400*.4)
            # Application parents must be inserted before child records at the same timestamp.
            states(f"{process.lower()}_applications", app, last)
    ordered = sorted(events, key=lambda e: (e[0], 0 if e[2].endswith("_applications") and e[3]["revision"] == 1 else 1, e[1]))
    for when, _, table, row in ordered:
        # Ordered by event time, so the cutoff ends the stream rather than filtering it.
        if as_of and when > as_of:
            return
        yield table, row


def connect():
    import pymssql
    return pymssql.connect(server=os.environ.get("SQLSERVER_HOST", "127.0.0.1"),
                           port=int(os.environ.get("SQLSERVER_PORT", "14333")),
                           user=os.environ["SQLSERVER_USER"], password=os.environ["SQLSERVER_PASSWORD"],
                           database=os.environ.get("SQLSERVER_DATABASE", "NDAStreaming"),
                           login_timeout=10, timeout=30, autocommit=False)


def write_state(conn, table, row):
    if table not in table_names():
        raise ValueError("Unknown transaction table")
    fields = list(COLUMNS)
    # Serializable key-range lock makes repeat delivery safe without MERGE races.
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT revision FROM dbo.{table} WITH (UPDLOCK,HOLDLOCK) WHERE record_id=%s", (row["record_id"],))
        current = cursor.fetchone()
        if current and current[0] >= row["revision"]:
            conn.commit()
            return False
        if current:
            cursor.execute(f"UPDATE dbo.{table} SET " + ",".join(f"{k}=%s" for k in fields if k != "record_id") + " WHERE record_id=%s",
                           tuple(row[k] for k in fields if k != "record_id") + (row["record_id"],))
        else:
            cursor.execute(f"INSERT INTO dbo.{table} ({','.join(fields)}) VALUES ({','.join(['%s']*len(fields))})", tuple(row[k] for k in fields))
    # Seeded applications claim their id too, so a user submission cannot collide with one.
    if table.endswith("_applications") and not current:
        from .submission import claim_simulated
        claim_simulated(conn, row["record_id"], row["process_code"], row["received_at"])
    conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=30, help="Applications per process; 0 admits them for the whole horizon")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--as-of", default=None, help="Simulation clock cutoff; unfinished work stays open")
    parser.add_argument("--arrival-days", type=float, default=ARRIVAL_DAYS, help="Simulated days between arrivals per process")
    parser.add_argument("--follow", action="store_true", help="Keep advancing the clock so new applications keep arriving")
    parser.add_argument("--days-per-second", type=float, default=.5, help="Simulated days per wall-clock second while following")
    parser.add_argument("--interval", type=float, default=.25, help="Wall-clock seconds between committed state changes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.count <= MAX_INTAKE or args.interval < 0:
        parser.error(f"count must be 0..{MAX_INTAKE} and interval non-negative")
    if args.arrival_days <= 0 or args.days_per_second <= 0:
        parser.error("--arrival-days and --days-per-second must be positive")
    start = datetime.fromisoformat(args.start)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    if as_of and as_of <= start:
        parser.error("--as-of must be after --start")
    if args.count == 0 and not as_of:
        parser.error("--count 0 needs --as-of to bound intake")
    if args.follow and not as_of:
        parser.error("--follow needs --as-of as the clock's starting point")
    conn = None if args.dry_run else connect()
    count = applications = 0
    try:
        if args.follow:
            print(f"Following from {as_of:%Y-%m-%d} at {args.days_per_second} simulated days/second; Ctrl+C to stop.", flush=True)
            began = time.monotonic()
            caught_up = False
            while True:
                horizon = as_of + timedelta(days=(time.monotonic() - began) * args.days_per_second)
                # Backfill runs on an accelerated clock only until it reaches the present.
                # From then on the clock is real time, so arrivals land on real dates at a
                # real rate rather than the simulation racing into the future.
                now = datetime.now()
                if horizon >= now:
                    horizon = now
                    if not caught_up:
                        caught_up = True
                        print(f"Backfill complete at {now:%Y-%m-%d %H:%M}; following real time "
                              f"(~1 arrival per process every {args.arrival_days} days).", flush=True)
                fresh = 0
                # The stream is a prefix extension, so already-written states are skipped locally.
                for index, (table, row) in enumerate(lifecycles(args.count, args.seed, start, horizon, args.arrival_days)):
                    if index < count:
                        continue
                    if conn:
                        write_state(conn, table, row)
                    count += 1
                    fresh += 1
                    applications += table.endswith("_applications") and row["revision"] == 1
                if fresh:
                    # Unbuffered: a follower is watched through its log, not its exit code.
                    print(f"  {horizon:%Y-%m-%d %H:%M} +{fresh} states; {count} total, {applications} applications received", flush=True)
                time.sleep(max(args.interval, 1))
        for table, row in lifecycles(args.count, args.seed, start, as_of, args.arrival_days):
            if conn:
                write_state(conn, table, row)
                time.sleep(args.interval)
            count += 1
            applications += table.endswith("_applications") and row["revision"] == 1
        print(f"{'Validated' if args.dry_run else 'Committed/replayed'} {count} state changes; {applications} synthetic applications.")
    except KeyboardInterrupt:
        print("Stopped; " + f"{count} state changes, {applications} applications received.")
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
