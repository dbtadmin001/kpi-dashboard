"""Offline coherence checks: does this tree hang together before anything runs?

Everything here works with no services, no network and no credentials, so it can
gate a pull request in seconds and run as the devcontainer's post-create step.
It deliberately does NOT test behaviour - delivery/integration.py does that
against real engines. This answers the cheaper question first: are the locks
actually locked, do the modules import, do the generated artefacts still agree
with the declarations they come from, and do the deployment guards still refuse
what they are supposed to refuse.

    python -m delivery.validate
"""
import importlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _check(results, name, condition, detail=""):
    results.append((bool(condition), name, detail))
    return bool(condition)


def modules(results):
    """Every module imports. Catches a syntax error or bad import in one second."""
    for module in ("streaming.contracts", "streaming.simulator", "streaming.api",
                   "marketplace.products", "marketplace.build", "marketplace.identity",
                   "marketplace.auth", "marketplace.tls", "marketplace.trino_config",
                   "marketplace.audit", "marketplace.retention",
                   "delivery.compose", "delivery.wait"):
        try:
            importlib.import_module(module)
            _check(results, f"import {module}", True)
        except Exception as error:                  # noqa: BLE001 - reporting, not handling
            _check(results, f"import {module}", False, f"{type(error).__name__}: {error}")


def supply_chain(results):
    """Locks must actually pin. An unpinned lock file is worse than none, because
    it looks like provenance and is not."""
    lock = (ROOT / "delivery/requirements.lock").read_text(encoding="utf-8")
    pinned = len(re.findall(r"^\S+==", lock, re.M))
    hashes = lock.count("--hash=sha256:")
    _check(results, "requirements.lock pins every package", pinned > 0 and hashes >= pinned,
           f"{pinned} packages, {hashes} hashes")
    _check(results, "requirements.lock was generated with --generate-hashes",
           "--generate-hashes" in lock)

    images = json.loads((ROOT / "delivery/images.lock.json").read_text(encoding="utf-8"))
    unpinned = [k for k, v in images.items() if k != "platform" and not DIGEST.search(v)]
    _check(results, "every base image pinned by digest", not unpinned, ", ".join(unpinned))

    jars = json.loads((ROOT / "delivery/jars.lock.json").read_text(encoding="utf-8"))
    bad = [j["name"] for j in jars if not re.fullmatch(r"[0-9a-f]{64}", j.get("sha256", ""))]
    _check(results, "every JVM artifact has a SHA256", not bad, ", ".join(bad))


def build_inputs(results):
    """Everything the Dockerfile COPYs exists. A missing path fails the build
    minutes in, after the dependency install."""
    dockerfile = (ROOT / "delivery/Dockerfile").read_text(encoding="utf-8")
    missing = []
    for line in dockerfile.splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        parts = line.split()[1:-1]
        missing += [p for p in parts if not (ROOT / p).exists()]
    _check(results, "Dockerfile COPY sources all exist", not missing, ", ".join(missing))


def deployment_guards(results):
    """The refusals that stop a bad deploy must still refuse."""
    from delivery.compose import topology

    tagged = {"app": "a:1", "flink": "f:1", "catalog": "c:1"}
    digested = {k: f"{v}@sha256:{'0' * 64}" for k, v in tagged.items()}
    cases = [
        ("rejects a non-isolated project name", "my-stack", tagged, {}),
        ("rejects tag-only images in production", "nda-production", tagged,
         {"production": True, "keycloak_url": "https://sso.example.org"}),
        ("rejects plain-HTTP Keycloak in production", "nda-production", digested,
         {"production": True, "keycloak_url": "http://sso.example.org"}),
        ("rejects a missing Keycloak URL in production", "nda-production", digested,
         {"production": True}),
    ]
    for name, project, images, kwargs in cases:
        try:
            topology(project, "/tmp/runtime", images, **kwargs)
            _check(results, name, False, "it was ACCEPTED")
        except ValueError:
            _check(results, name, True)

    spec = topology("nda-ci-validate", "/tmp/runtime", digested,
                    production=True, keycloak_url="https://sso.example.org")
    _check(results, "a correct production topology still renders",
           len(spec["services"]) > 10, f"{len(spec['services'])} services")
    # The audit receiver must be in the topology, or the event listener has to be
    # switched off and the one control nobody tests is the one that records access.
    _check(results, "audit receiver is part of the stack", "audit" in spec["services"])
    _check(results, "audit log is written to the mounted volume",
           spec["services"]["audit"]["environment"]["AUDIT_LOG_PATH"].startswith("/run/nda"))


def governance(results):
    """The access-rule invariants, checked without a cluster."""
    from marketplace.build import access_rules

    rules = access_rules()
    named = [r for section in ("catalogs", "schemas", "tables")
             for r in rules[section]
             if "user" in r and not r.get("schema", "").startswith("sandbox_")]
    _check(results, "no production grant names a person", not named, str(named[:2]))
    _check(results, "tables default to deny", rules["tables"][-1] == {"privileges": []})
    _check(results, "impersonation denied unless granted",
           all(r["allow"] is False for r in rules["impersonation"]))

    masked = [r for r in rules["tables"] if "columns" in r]
    _check(results, "entity_id is masked on the granting rule", masked and
           all(r["privileges"] == ["SELECT"] for r in masked), f"{len(masked)} masked rules")


def kubernetes(results):
    """The manifests render, and render to something Kubernetes will accept.

    Offline, so this cannot call an API server - but it can catch the two
    classes of mistake that reached one last time: a volumeMount naming a volume
    the pod never declares, and a bare scalar that Go's YAML reads as a boolean
    when Python's does not.
    """
    import yaml
    from delivery.kubernetes import manifests

    documents = list(manifests(nodes=3))
    _check(results, "manifests render", len(documents) > 30, f"{len(documents)} resources")

    # Every volumeMount must name a volume the pod declares.
    dangling = []
    for doc in documents:
        if doc["kind"] not in ("Deployment", "StatefulSet"):
            continue
        pod = doc["spec"]["template"]["spec"]
        declared = {v["name"] for v in pod.get("volumes", [])}
        declared |= {c["metadata"]["name"] for c in doc["spec"].get("volumeClaimTemplates", [])}
        for container in pod.get("initContainers", []) + pod["containers"]:
            for mount in container.get("volumeMounts", []):
                if mount["name"] not in declared:
                    dangling.append(f"{doc['metadata']['name']}/{container['name']}:{mount['name']}")
    _check(results, "every volumeMount names a declared volume", not dangling, ", ".join(dangling))

    # Round-trip: what we wrote must read back as what we meant. `ACCEPT_EULA:
    # Y` was emitted bare and Kubernetes read it as `true`.
    from delivery.kubernetes import render
    import io, contextlib, tempfile
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as handle:
        path = handle.name
    with contextlib.redirect_stdout(io.StringIO()):
        render(3, None, "http://nda-keycloak:8180", out=path)
    reloaded = list(yaml.safe_load_all(pathlib.Path(path).read_text(encoding="utf-8")))
    wrong = []
    for doc in reloaded:
        if doc and doc["kind"] in ("Deployment", "StatefulSet"):
            pod = doc["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                for env in container.get("env", []):
                    if "value" in env and not isinstance(env["value"], str):
                        wrong.append(f"{doc['metadata']['name']}/{env['name']}={env['value']!r}")
    _check(results, "env values round-trip as strings", not wrong, ", ".join(wrong))
    pathlib.Path(path).unlink(missing_ok=True)

    # Every image must resolve; a KeyError here means the lock is missing one.
    names = {c["image"] for doc in documents if doc["kind"] in ("Deployment", "StatefulSet")
             for c in doc["spec"]["template"]["spec"].get("initContainers", [])
             + doc["spec"]["template"]["spec"]["containers"]}
    _check(results, "every workload names an image", all(names), f"{len(names)} distinct")


def main():
    results = []
    modules(results)
    supply_chain(results)
    build_inputs(results)
    deployment_guards(results)
    kubernetes(results)
    governance(results)

    width = max(len(name) for _, name, _ in results)
    for ok, name, detail in results:
        suffix = f"  {detail}" if detail else ""
        print(f"  [{'ok' if ok else 'FAIL'}] {name:{width}}{suffix}")
    failed = [name for ok, name, _ in results if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} checks failed")
        return 1
    print(f"All {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
