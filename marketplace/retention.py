"""Sandbox retention: keep the near-zero storage promise honest.

Views in a sandbox cost about 600 bytes and can be left alone forever. Derived
TABLES cost real bytes, and without a policy they accumulate silently until the
warehouse is full of someone's abandoned experiment from March.

This enforces two limits:

  * **Age.** A derived table untouched for TTL_DAYS is dropped.
  * **Size.** A sandbox over QUOTA_MB has its largest tables dropped, oldest
    first, until it fits.

Dropping an Iceberg table removes its metadata immediately, but the data files
are only reclaimed once snapshots expire, so this expires them too. Reporting is
the default; destroying anything requires --enforce.

    python -m marketplace.retention report
    python -m marketplace.retention enforce --confirm
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

from .products import SANDBOX_PREFIX

TTL_DAYS = int(os.environ.get("SANDBOX_TTL_DAYS", "30"))
QUOTA_MB = int(os.environ.get("SANDBOX_QUOTA_MB", "5120"))


def connection(user="marketplace_owner"):
    from .auth import connect_any
    return connect_any(user, catalog="iceberg")


def run(conn, sql):
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        cursor.close()


def sandboxes(conn):
    rows = run(conn, "SHOW SCHEMAS FROM iceberg")
    return sorted(r[0] for r in rows if r[0].startswith(SANDBOX_PREFIX))


def contents(conn, schema):
    """Tables and views in one sandbox, with size and last change for tables."""
    rows = run(conn, f"""
        SELECT table_name, table_type FROM iceberg.information_schema.tables
        WHERE table_schema = '{schema}'""")
    items = []
    for name, kind in rows:
        if kind == "VIEW":
            items.append({"name": name, "kind": "VIEW", "megabytes": 0.0, "last_changed": None})
            continue
        megabytes, last_changed = 0.0, None
        try:
            stats = run(conn, f"""
                SELECT COALESCE(SUM(total_size), 0) / 1048576.0, MAX(committed_at)
                FROM iceberg.{schema}."{name}$snapshots" s
                LEFT JOIN iceberg.{schema}."{name}$files" f ON true""")
            if stats and stats[0][0] is not None:
                megabytes = float(stats[0][0])
                last_changed = stats[0][1]
        except Exception:
            # A half-written table should be reported, not crash the sweep.
            pass
        items.append({"name": name, "kind": "TABLE",
                      "megabytes": round(megabytes, 1), "last_changed": last_changed})
    return items


def plan(conn):
    """What would be dropped, and why. Nothing is destroyed here."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
    actions = []
    for schema in sandboxes(conn):
        items = contents(conn, schema)
        tables = [i for i in items if i["kind"] == "TABLE"]
        total = sum(i["megabytes"] for i in tables)
        for item in tables:
            changed = item["last_changed"]
            if changed is not None and changed.replace(tzinfo=timezone.utc) < cutoff:
                actions.append((schema, item, f"untouched for over {TTL_DAYS} days"))
        # Oldest first until the sandbox fits under quota.
        if total > QUOTA_MB:
            remaining = total
            for item in sorted(tables, key=lambda i: (i["last_changed"] or datetime.min.replace(tzinfo=timezone.utc))):
                if remaining <= QUOTA_MB:
                    break
                if any(a[1]["name"] == item["name"] and a[0] == schema for a in actions):
                    continue
                actions.append((schema, item, f"sandbox over {QUOTA_MB} MB quota"))
                remaining -= item["megabytes"]
    return actions


def report(conn):
    schemas = sandboxes(conn)
    if not schemas:
        print("No sandboxes yet.")
        return []
    print(f"{'sandbox':28} {'views':>5} {'tables':>6} {'MB':>8}")
    for schema in schemas:
        items = contents(conn, schema)
        views = sum(1 for i in items if i["kind"] == "VIEW")
        tables = [i for i in items if i["kind"] == "TABLE"]
        print(f"{schema:28} {views:>5} {len(tables):>6} {sum(i['megabytes'] for i in tables):>8.1f}")
    actions = plan(conn)
    print()
    if not actions:
        print(f"Nothing to reclaim. TTL {TTL_DAYS} days, quota {QUOTA_MB} MB per sandbox.")
    else:
        print(f"{len(actions)} table(s) would be dropped:")
        for schema, item, reason in actions:
            print(f"  {schema}.{item['name']:28} {item['megabytes']:>7.1f} MB  ({reason})")
    return actions


def enforce(conn, confirm=False):
    actions = plan(conn)
    if not actions:
        print("Nothing to reclaim.")
        return 0
    if not confirm:
        print(f"{len(actions)} table(s) would be dropped. Re-run with --confirm to do it.")
        return 0
    dropped = 0
    for schema, item, reason in actions:
        target = f'iceberg.{schema}."{item["name"]}"'
        try:
            # Expire first so the files actually go, then drop the table.
            run(conn, f"ALTER TABLE {target} EXECUTE expire_snapshots(retention_threshold => '0d')")
        except Exception:
            pass
        run(conn, f"DROP TABLE IF EXISTS {target}")
        print(f"  dropped {schema}.{item['name']} ({item['megabytes']} MB) - {reason}")
        dropped += 1
    print(f"Reclaimed {dropped} table(s)")
    return dropped


def main():
    parser = argparse.ArgumentParser(description="Sandbox retention")
    parser.add_argument("action", choices=["report", "enforce"])
    parser.add_argument("--confirm", action="store_true",
                        help="Actually drop; without it the run is a preview")
    args = parser.parse_args()
    conn = connection()
    try:
        if args.action == "report":
            report(conn)
        else:
            enforce(conn, args.confirm)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
