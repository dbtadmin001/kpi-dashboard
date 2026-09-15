"""Promote artifacts between environments. Never rebuild for one.

The rule this enforces is the whole point: **the thing you tested is the thing
you ship.** A rebuild produces different bytes, so a pipeline that builds per
environment has tested nothing that reaches production. Here an artifact is a
set of image digests, built once, and promotion moves that exact set forward.

    ephemeral  ->  staging  ->  production

An artifact may only enter an environment if it was VERIFIED in the one before.
`verify` records that; `promote` refuses without it. Merging a pull request does
not deploy anything - it makes an artifact eligible.

Rolling back is promoting an artifact that already passed, which is instant
because the images are already built and already pulled.

    python -m delivery.promote status
    python -m delivery.promote record --sha <git-sha>      # after a build
    python -m delivery.promote verify <artifact> --env staging
    python -m delivery.promote promote <artifact> --to production
    python -m delivery.promote rollback --env production

WHAT CAN AND CANNOT BE PROMOTED THIS WAY

Stateless services - the API, the Trino coordinator, the audit collector, the
identity sync - can be replaced wholesale, which is what makes blue/green and an
instant rollback possible.

The data plane cannot. Postgres, MinIO, Kafka and SQL Server hold the state the
platform exists to serve; you cannot stand a second copy beside them and switch.
Those move forward in place, with migrations, and a rollback there is a restore -
a different operation with a different risk profile. Pretending otherwise is how
a "simple rollback" turns into an outage.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The ladder. An artifact enters an environment only from the one before it.
LADDER = ["ephemeral", "staging", "production"]

# Services that can be replaced wholesale, and those that hold state.
STATELESS = ("api", "trino", "audit", "identity", "opa", "iceberg-rest", "keycloak")
STATEFUL = ("postgres", "minio", "kafka", "sqlserver", "jobmanager", "taskmanager", "connect")


def _store():
    from catalog import store
    return store.connect()


def artifact_key(images: dict) -> str:
    """Name an artifact by what it actually contains, not by a tag.

    Two builds of the same commit produce different digests, and that difference
    is the thing that matters - so the key is derived from the digests.
    """
    import hashlib
    digests = "|".join(f"{k}={images[k]}" for k in sorted(images))
    return hashlib.sha256(digests.encode()).hexdigest()[:12]


def current_images():
    path = ROOT / "delivery" / "images.built.json"
    if not path.exists():
        raise SystemExit("No images.built.json. Run: python -m delivery.release build")
    return json.loads(path.read_text(encoding="utf-8"))


def git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()[:12]
    except OSError:
        return "unknown"


# --------------------------------------------------------------------------
def record(images=None, sha=None, source="local"):
    """Register a freshly built artifact. This does not deploy it anywhere."""
    from catalog import store
    images = images or current_images()
    digested = [k for k in ("app", "flink", "catalog") if "@sha256:" in images.get(k, "")]
    key = artifact_key(images)
    conn = _store()
    store.put_artifact(conn, key, sha or git_sha(), images, source,
                       immutable=len(digested) == 3)
    conn.commit()
    print(f"Recorded artifact {key}")
    print(f"  commit    {sha or git_sha()}")
    print(f"  immutable {'yes' if len(digested) == 3 else 'NO - tag-only, cannot reach production'}")
    for name, reference in images.items():
        print(f"  {name:9} {reference}")
    conn.close()
    return key


def verify(key, environment, passed=True, detail=""):
    """Record that an artifact was proven in an environment.

    This is what promotion checks. It is deliberately a separate step from
    deploying: an artifact can be running somewhere and not yet be verified.
    """
    from catalog import store
    if environment not in LADDER:
        raise SystemExit(f"Unknown environment {environment!r}. One of: {', '.join(LADDER)}")
    conn = _store()
    if not store.get_artifact(conn, key):
        raise SystemExit(f"No artifact {key}. Record it first.")
    store.put_verification(conn, key, environment, "passed" if passed else "failed", detail)
    conn.commit()
    print(f"{'PASSED' if passed else 'FAILED'} {key} in {environment}"
          + (f" - {detail}" if detail else ""))
    conn.close()


def promote(key, to_env, actor=None, force=False):
    """Move an artifact into an environment, if it earned the right to be there."""
    from catalog import store
    if to_env not in LADDER:
        raise SystemExit(f"Unknown environment {to_env!r}")
    index = LADDER.index(to_env)
    conn = _store()

    artifact = store.get_artifact(conn, key)
    if not artifact:
        raise SystemExit(f"No artifact {key}")

    # Production takes immutable references only. A tag can be moved under you;
    # a digest cannot.
    if to_env == "production" and not artifact["immutable"]:
        raise SystemExit(
            f"{key} is tag-only and cannot enter production.{os.linesep}"
            "  Build with a registry so the images have digests:"
            f"{os.linesep}    NDA_REGISTRY=ghcr.io/<owner> python -m delivery.release build")

    # The gate: verified in the previous rung.
    if index > 0 and not force:
        previous = LADDER[index - 1]
        proof = store.get_verification(conn, key, previous)
        if not proof or proof["status"] != "passed":
            raise SystemExit(
                f"{key} has not passed verification in {previous}.{os.linesep}"
                f"  Promotion is not a shortcut around testing - verify it there first:"
                f"{os.linesep}    python -m delivery.promote verify {key} --env {previous}")

    previous_state = store.get_environment(conn, to_env)
    store.put_promotion(conn, key, LADDER[index - 1] if index else "build", to_env,
                        actor or os.environ.get("USER") or "operator",
                        "promoted", json.dumps({"replaced": (previous_state or {}).get("artifact")}))
    store.put_environment(conn, to_env, key, "deploying")
    conn.commit()
    print(f"Promoted {key} into {to_env}")
    if previous_state and previous_state.get("artifact"):
        print(f"  replaces {previous_state['artifact']} (still available to roll back to)")
    print(f"  stateless services replaced wholesale: {', '.join(STATELESS)}")
    print(f"  state carried forward in place:        {', '.join(STATEFUL)}")
    conn.close()
    return key


def rollback(environment, actor=None):
    """Return an environment to the artifact it ran before this one.

    Instant, because the images are already built and already pulled. This moves
    the stateless half only - anything a migration changed stays changed.
    """
    from catalog import store
    conn = _store()
    history = store.promotions(conn, environment, limit=10)
    if len(history) < 2:
        raise SystemExit(f"{environment} has no previous artifact to return to.")
    target = history[1]["artifact"]
    store.put_promotion(conn, target, environment, environment,
                        actor or "operator", "rolled-back",
                        json.dumps({"from": history[0]["artifact"]}))
    store.put_environment(conn, environment, target, "deploying")
    conn.commit()
    print(f"Rolled {environment} back to {target}")
    print("  NOTE: this returns the stateless services only. Anything a database")
    print("  migration changed is still changed; that is a restore, not a rollback.")
    conn.close()
    return target


def status():
    from catalog import store
    conn = _store()
    print("Environments")
    for environment in LADDER:
        state = store.get_environment(conn, environment)
        if not state or not state.get("artifact"):
            print(f"  {environment:11} (nothing deployed)")
            continue
        artifact = store.get_artifact(conn, state["artifact"]) or {}
        age = time.time() - (state.get("since") or time.time())
        print(f"  {environment:11} {state['artifact']}  commit {artifact.get('git_sha','?')}  "
              f"{state.get('status','?')}  {int(age // 60)}m")
    print()
    print("Recent artifacts")
    for a in store.artifacts(conn, limit=8):
        marks = []
        for environment in LADDER:
            proof = store.get_verification(conn, a["key"], environment)
            if proof:
                marks.append(f"{environment[:4]}:{'ok' if proof['status'] == 'passed' else 'FAIL'}")
        print(f"  {a['key']}  {a['git_sha']:12} {'digest' if a['immutable'] else 'tag   '}  "
              f"{' '.join(marks) or 'unverified'}")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Promote artifacts between environments")
    parser.add_argument("action", choices=["status", "record", "verify", "promote", "rollback"])
    parser.add_argument("artifact", nargs="?")
    parser.add_argument("--env", default="staging")
    parser.add_argument("--to", dest="to_env")
    parser.add_argument("--sha")
    parser.add_argument("--actor")
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--detail", default="")
    parser.add_argument("--force", action="store_true",
                        help="Skip the gate. Recorded as forced, and visible as such.")
    args = parser.parse_args()

    if args.action == "status":
        status()
    elif args.action == "record":
        record(sha=args.sha)
    elif args.action == "verify":
        verify(args.artifact, args.env, not args.failed, args.detail)
    elif args.action == "promote":
        promote(args.artifact, args.to_env or args.env, args.actor, args.force)
    else:
        rollback(args.env, args.actor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
