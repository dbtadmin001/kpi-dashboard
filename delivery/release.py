"""Build, verify, deploy. The contract between them, written down and executable.

`compose.topology()` consumes three images it does not build, and the Dockerfile
builds three images nothing consumed. This is the missing half: it builds them
from pinned bases, resolves their immutable digests, proves them against a
disposable stack, and only then renders a deployment.

    python -m delivery.release build                     # -> delivery/images.built.json
    python -m delivery.release verify                    # isolated stack + full gate
    python -m delivery.release render --keycloak-url https://sso.example.org
    python -m delivery.release deploy  --keycloak-url https://sso.example.org --confirm

**Digests only exist after a push.** A locally built image has no repository
digest, so `build` without a registry produces tags, which `topology(production=
True)` rightly refuses. Set NDA_REGISTRY to push and get digests back. That is
not a limitation to work around: it is what makes a deployment reproducible.
"""
import argparse
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILT = ROOT / "delivery/images.built.json"
STAGES = ("app", "flink", "catalog")


def _run(command, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("cwd", ROOT)
    return subprocess.run(command, **kwargs)


def _capture(command, **kwargs):
    return _run(command, capture_output=True, text=True, **kwargs).stdout.strip()


def base_images():
    return json.loads((ROOT / "delivery/images.lock.json").read_text(encoding="utf-8"))


def build(registry=None, tag=None):
    """Build each stage from digest-pinned bases; push if a registry is given."""
    base = base_images()
    registry = registry or os.environ.get("NDA_REGISTRY", "")
    tag = tag or os.environ.get("NDA_TAG") or time.strftime("%Y%m%d-%H%M%S")
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    built = {}
    for stage in STAGES:
        reference = f"{prefix}nda-{stage}:{tag}"
        print(f"--- building {stage} -> {reference}")
        _run(["docker", "build", "-f", "delivery/Dockerfile", "--target", stage,
              "--platform", base["platform"], "-t", reference,
              "--build-arg", f"PYTHON_IMAGE={base['python']}",
              "--build-arg", f"FLINK_IMAGE={base['flink']}",
              "--build-arg", f"CATALOG_IMAGE={base['catalog_base']}",
              "."])
        if registry:
            _run(["docker", "push", reference])
            # RepoDigests is populated by the push, and is the only immutable name.
            digests = json.loads(_capture(
                ["docker", "image", "inspect", reference, "--format", "{{json .RepoDigests}}"]))
            match = [d for d in digests if d.startswith(prefix)]
            if not match:
                raise SystemExit(f"{reference} has no repository digest after push")
            reference = match[0]
        built[stage] = reference
    BUILT.write_text(json.dumps(built, indent=2) + "\n", encoding="utf-8")
    print(f"{chr(10)}Wrote {BUILT}")
    for stage, reference in built.items():
        print(f"  {stage:8} {reference}")
    if not registry:
        print(f"{chr(10)}No registry set, so these are tags, not digests. `deploy` will")
        print("refuse them. Set NDA_REGISTRY to push and get immutable references.")
    return built


def built_images():
    if not BUILT.exists():
        raise SystemExit(f"{BUILT} does not exist. Run: python -m delivery.release build")
    return json.loads(BUILT.read_text(encoding="utf-8"))


def _write_topology(name, runtime, images, out=None, **kwargs):
    from .compose import topology
    import yaml
    try:
        spec = topology(name, runtime, images, **kwargs)
    except ValueError as error:
        # These are the deployment guards refusing, which is a result, not a crash.
        raise SystemExit(f"Refused: {error}") from None
    # `runtime` is baked into the volume mounts, so it must be the path on the
    # HOST THAT WILL RUN THIS. `out` is merely where the file is written now -
    # which is how CI can render a deployment for a VM it is not running on.
    target = pathlib.Path(out) if out else pathlib.Path(runtime) / "compose.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return target, spec


def verify(keep=False):
    """Stand up a disposable stack, run every gate against it, tear it down.

    The project name is unique per run, so this can never touch staging or
    production state, and `bootstrap.require_ci` refuses to provision without the
    marker written below.
    """
    from .secrets import init as init_secrets

    project = f"nda-ci-{secrets.token_hex(4)}"
    runtime = ROOT / ".ci" / project
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "ci-marker.json").write_text(json.dumps({"project": project}), encoding="utf-8")
    init_secrets(runtime)

    compose_file, _ = _write_topology(project, runtime, built_images())
    compose = ["docker", "compose", "-f", str(compose_file), "-p", project]
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    try:
        # Trino, OPA and their dependants consume files rendered by the bootstrap
        # step. Starting every service first made Trino restart forever with an
        # empty /etc/trino and made OPA fail because policy files were absent.
        # Bring up only the engines that bootstrap needs, render the runtime
        # configuration, then start the authenticated serving plane.
        foundation = ["postgres", "minio", "kafka", "sqlserver", "connect",
                      "iceberg-rest", "jobmanager", "taskmanager", "keycloak"]
        _run(compose + ["up", "-d", "--wait", *foundation], env=env)
        for step in (["identities"], ["source"]):
            _run(compose + ["run", "--rm", "--no-deps", "tools", "python", "-m",
                            "delivery.bootstrap", *step], env=env)
        _run(compose + ["up", "-d", "--wait"], env=env)          # read the rendered config
        _run(compose + ["run", "--rm", "--no-deps", "tools"], env=env) # the integration gate
        print(f"{chr(10)}Verified {project}")
        return True
    finally:
        if keep:
            print(f"Stack left running: docker compose -p {project} down -v")
        else:
            _run(compose + ["down", "-v", "--remove-orphans"], env=env, check=False)


def render(keycloak_url, runtime, name="nda-production", out=None):
    images = built_images()
    target, spec = _write_topology(name, runtime, images, out=out, production=True,
                                   keycloak_url=keycloak_url)
    print(f"Rendered {len(spec['services'])} services to {target}")
    return target


def deploy(keycloak_url, runtime, confirm=False, name="nda-production"):
    from .secrets import check as check_secrets

    if check_secrets(runtime) != 0:
        raise SystemExit("Refusing to deploy without a complete secrets.env")
    target = render(keycloak_url, runtime, name)
    if not confirm:
        print(f"{chr(10)}Dry run. Nothing started. To apply:")
        print(f"  docker compose -f {target} -p {name} up -d --wait")
        print("  (or re-run this with --confirm)")
        return 0
    _run(["docker", "compose", "-f", str(target), "-p", name, "up", "-d", "--wait"],
         env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    print(f"{name} is up")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Build, verify and deploy the platform")
    parser.add_argument("action", choices=["build", "verify", "render", "deploy"])
    parser.add_argument("--registry", default=None, help="Push here to obtain digests")
    parser.add_argument("--tag", default=None)
    parser.add_argument("--keycloak-url", default=os.environ.get("NDA_KEYCLOAK_URL"))
    # /var/lib, not /run: this directory holds secrets.env, the OIDC client, the
    # TLS keystore, the rendered Trino config and the audit log. /run is a tmpfs
    # on every systemd host, so putting them there loses all of it on reboot.
    parser.add_argument("--runtime", default=os.environ.get("NDA_RUNTIME", "/var/lib/nda"))
    parser.add_argument("--keep", action="store_true", help="Leave the CI stack running")
    parser.add_argument("--confirm", action="store_true", help="Actually start the deployment")
    parser.add_argument("--out", default=None,
                        help="Write the rendered compose here instead of into --runtime")
    args = parser.parse_args()

    if args.action == "build":
        build(args.registry, args.tag)
    elif args.action == "verify":
        verify(args.keep)
    else:
        if not args.keycloak_url:
            raise SystemExit("--keycloak-url is required (the public HTTPS URL of Keycloak)")
        if args.action == "render":
            render(args.keycloak_url, args.runtime, out=args.out)
        else:
            return deploy(args.keycloak_url, args.runtime, args.confirm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
