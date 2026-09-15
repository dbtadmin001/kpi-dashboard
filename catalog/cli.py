"""Collect, inspect and serve the catalog.

    python -m catalog.cli collect            # every collector
    python -m catalog.cli collect --only trino keycloak
    python -m catalog.cli health
    python -m catalog.cli stats
    python -m catalog.cli impact <urn>
    python -m catalog.cli serve
"""
import argparse
import json

from . import store
from .collectors import REGISTRY, all_collectors, health as collector_health
from .resolver import relink, run_collector


def collect(names=None):
    conn = store.connect()
    print(f"Collecting into {store.DEFAULT_PATH.name}")
    results = [run_collector(conn, c) for c in all_collectors(names)]
    print("  relinking cross-provider edges...")
    edges = relink(conn)
    print(f"  {edges} edges resolved across providers")
    summary = store.stats(conn)
    print(f"{chr(10)}  {summary['resources']} resources, {summary['edges']} edges, "
          f"{summary['aliases']} aliases")
    conn.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="Infrastructure catalog")
    parser.add_argument("action", choices=["collect", "health", "stats", "impact", "serve", "search"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("--only", nargs="*", choices=list(REGISTRY))
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args()

    if args.action == "collect":
        collect(args.only)
    elif args.action == "health":
        for h in collector_health():
            print(f"  {'ok  ' if h['ok'] else 'FAIL'} {h['plugin']:12} {h['detail'][:80]}")
    elif args.action == "stats":
        conn = store.connect()
        s = store.stats(conn)
        print(f"  {s['resources']} resources, {s['edges']} edges, {s['stale']} stale")
        for row in s["by_provider"]:
            print(f"    {row['value']:14} {row['n']:4}")
    elif args.action == "search":
        conn = store.connect()
        found = store.search(conn, q=args.target, limit=25)
        print(f"  {found['total']} matches")
        for item in found["items"]:
            print(f"    {item['kind']:22} {item['name'][:56]:58} {item['provider']}")
    elif args.action == "impact":
        conn = store.connect()
        result = store.impact(conn, args.target)
        print(f"  {result['count']} dependents, {result['low_confidence']} low-confidence")
        for item in result["items"][:40]:
            print(f"    {item['hops']} hop  {(item['kind'] or '?'):20} {(item['name'] or item['urn'])[:60]}")
    else:
        from .api import serve
        serve(args.port)


if __name__ == "__main__":
    main()
