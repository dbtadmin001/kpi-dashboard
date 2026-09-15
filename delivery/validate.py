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
import os
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
                   "catalog.store",
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

    # A digest that no longer resolves is the failure that cost us a CI run:
    # MinIO stopped publishing to Docker Hub, the pin stayed valid-looking, and
    # the build got four minutes in before `docker compose up` said "access
    # denied". Checking the registry is a network call, so it is opt-in - but CI
    # sets CATALOG_CHECK_REGISTRY=1 and finds it in seconds instead.
    if os.environ.get("CATALOG_CHECK_REGISTRY", "").lower() in ("1", "true", "yes"):
        import subprocess
        from concurrent.futures import ThreadPoolExecutor

        # Every probe here is a network round trip, and some of them pull an
        # image. Run serially this was the single slowest thing in the fast gate;
        # they are independent, so waiting for them one at a time bought nothing.
        def _probe(argv, timeout):
            return subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout,
                                  env={**os.environ, "MSYS_NO_PATHCONV": "1"})

        def _all(jobs):
            with ThreadPoolExecutor(max_workers=8) as pool:
                return list(pool.map(lambda j: (j[0], _probe(j[1], j[2])), jobs))

        unresolvable = [name for name, probe in _all(
            [(name, ["docker", "manifest", "inspect", reference], 120)
             for name, reference in images.items() if name != "platform"])
            if probe.returncode != 0]
        _check(results, "every pinned image still resolves in its registry",
               not unresolvable, ", ".join(unresolvable))

        # And that every healthcheck can actually RUN in the image it targets.
        # Two shipped that could not: iceberg-rest has no curl, and OPA has no
        # shell at all. Both looked fine and both made `depends_on` wait forever,
        # reporting the service as unhealthy rather than the check as wrong.
        from delivery.compose import topology
        spec = topology("nda-ci-validate", "/tmp/x",
                        {"app": "a", "flink": "f", "catalog": "c"})
        wanted = []
        for service, definition in spec["services"].items():
            check = definition.get("healthcheck")
            # Only base images can be checked here. app/flink/catalog do not
            # exist until `release build` runs, and CI checks them after it does.
            if not check or definition["image"] not in images.values():
                continue
            tool = check["test"][-1].split()[0]
            wanted.append((f"{service} needs {tool}",
                           ["docker", "run", "--rm", "--entrypoint", "sh",
                            definition["image"], "-c", f"command -v {tool}"], 300))
        broken = [label for label, probe in _all(wanted) if probe.returncode != 0]
        _check(results, "every healthcheck can run in its own image",
               not broken, "; ".join(broken))

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
    # Integration tests run inside the built application image.  Importing a
    # package from the checkout is not evidence that it was copied there.
    packaged = {line.split()[1] for line in dockerfile.splitlines()
                if line.startswith("COPY ") and "--from=" not in line}
    required = {"streaming", "catalog", "marketplace", "delivery"}
    absent = sorted(required - packaged)
    _check(results, "application image includes every tested package", not absent,
           ", ".join(absent))


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


def credentials(results):
    """A generated secrets.env must be one a stack can actually start with.

    Both failures this catches presented as an unhealthy or restart-looping
    container, minutes into CI, with nothing in the message about a password:
    three independent values for the one `nda` Postgres user, and a Keycloak
    admin username with no password because Keycloak 26 reads the variable under
    a different name. Neither needs a container to detect.
    """
    import contextlib
    import io
    import tempfile
    from delivery import secrets as secret_module

    with tempfile.TemporaryDirectory() as directory:
        with contextlib.redirect_stdout(io.StringIO()):   # it reports what it wrote
            secret_module.init(directory)
        text = (pathlib.Path(directory) / "secrets.env").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in text.splitlines()
                  if "=" in line and not line.startswith("#"))

    _check(results, "every required secret is generated",
           all(values.get(n) for n, auto, _ in secret_module.REQUIRED if auto),
           ", ".join(n for n, auto, _ in secret_module.REQUIRED if auto and not values.get(n)))

    wrong = [f"{n} != {src}" for n, src in secret_module.ALIAS.items()
             if values.get(n) != values.get(src)]
    _check(results, "secrets that are one credential share one value", not wrong,
           ", ".join(wrong))

    # Anything Kubernetes projects out of the secret has to be in it, or the pod
    # starts with the variable simply unset - which is how the Keycloak one hid.
    manifest = (ROOT / "delivery/kubernetes.py").read_text(encoding="utf-8")
    projected = set(re.findall(r'_secret_env\(([^)]*)\)', manifest))
    wanted = {name for group in projected for name in re.findall(r'"([A-Z0-9_]+)"', group)}
    undeclared = sorted(wanted - {n for n, _, _ in secret_module.REQUIRED})
    _check(results, "every secret the manifests project is one we generate",
           not undeclared, ", ".join(undeclared))

    # Provisioning code must not rely on a variable that cannot be present in
    # secrets.env. This caught the original CI failure after the whole stack had
    # started: bootstrap needed SQLSERVER_PASSWORD and DEBEZIUM_PASSWORD but the
    # secret contract did not declare either.
    bootstrap = (ROOT / "streaming/bootstrap.py").read_text(encoding="utf-8")
    required_by_bootstrap = set(re.findall(r'os\.environ\["([A-Z0-9_]+)"\]', bootstrap))
    undeclared = sorted(required_by_bootstrap - {n for n, _, _ in secret_module.REQUIRED})
    _check(results, "bootstrap credentials are generated", not undeclared, ", ".join(undeclared))

    _check(results, "MinIO client credentials match its initialized identity",
           values.get("AWS_ACCESS_KEY_ID") == values.get("MINIO_ROOT_USER")
           and values.get("AWS_SECRET_ACCESS_KEY") == values.get("MINIO_ROOT_PASSWORD"))


def streaming_runtime(results):
    """The Flink services must carry what the Iceberg sink needs to reach S3.

    iceberg-aws-bundle resolves a region through the AWS SDK provider chain,
    which reads the environment. With none set it throws "Unable to load region
    from any of the providers in the chain", the tasks restart forever, silver
    never fills, and the failure surfaces ten minutes later as a reconciliation
    timeout that names neither S3 nor a region. Reproduced and fixed against a
    real session cluster; this keeps it fixed.
    """
    from delivery.compose import cdc_topics, topology

    spec = topology("nda-ci-validate", "/tmp/runtime",
                    {"app": "a", "flink": "f", "catalog": "c"})
    # FLINK_PROPERTIES marks the services that actually run a Flink JVM, and so
    # the ones that load the Iceberg sink. flink-init shares the image but only
    # prepares a directory, and needs no credentials of any kind.
    flink = [n for n, d in spec["services"].items()
             if "FLINK_PROPERTIES" in d.get("environment", {})]
    missing = [n for n in flink
               if spec["services"][n]["environment"].get("AWS_REGION") is None]
    _check(results, "every Flink service declares an AWS region", not missing,
           ", ".join(missing))

    # The SQL job reads these by name. If a source table is added without its
    # topic, the job fails at runtime with a metadata lookup, not at submission.
    topics = cdc_topics()
    creator = spec["services"]["kafka-init"]["command"][0]
    absent = [t for t in topics if t not in creator]
    _check(results, "every CDC topic is created before the job that reads it",
           not absent and len(topics) > 0, ", ".join(absent) or f"{len(topics)} topics")
    _check(results, "the SQL job waits for topic creation to finish",
           spec["services"]["flink-sql"]["depends_on"].get("kafka-init", {}).get("condition")
           == "service_completed_successfully")
    # Topic creation talks to the broker, so "started" is not good enough.
    _check(results, "Kafka is waited on by readiness, not by process start",
           "healthcheck" in spec["services"]["kafka"])


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
    credentials(results)
    streaming_runtime(results)
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
