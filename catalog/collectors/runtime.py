"""What is actually running: containers, networks, volumes, and Kubernetes.

Two collectors that answer the "observed" half of the catalog.

DockerCollector reads the local engine. On this estate that IS the runtime -
twenty-odd containers across three compose projects - and reading it costs
nothing, so the catalog is useful before any cluster exists.

KubernetesCollector prefers the live API and falls back to the rendered
manifests. The fallback is marked `declared`, not `observed`, because a manifest
is a statement of intent and the catalog must never present intent as fact.
"""
import json
import os
import pathlib
import subprocess
from typing import Iterable, List

from ..model import CONVENTIONAL, DECLARED, EXPLICIT, CandidateEdge, Entity, Reference


def _docker(*args) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=90, env={**os.environ, "MSYS_NO_PATHCONV": "1"}).stdout


class DockerCollector:
    name = "docker"
    provider = "docker"
    kinds = ["container", "network", "volume", "image", "compose_project"]
    capabilities = {"inventory", "health", "dependencies"}

    def health(self):
        out = _docker("version", "--format", "{{.Server.Version}}").strip()
        return {"ok": bool(out), "detail": f"engine {out}" if out else "engine unreachable"}

    def collect(self):
        raw = _docker("ps", "-a", "--format", "{{json .}}")
        containers = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not containers:
            raise RuntimeError("docker engine returned no containers")

        projects = {}
        for container in containers:
            detail = self._inspect(container["ID"])
            labels = (detail.get("Config", {}) or {}).get("Labels") or {}
            project = labels.get("com.docker.compose.project") or "standalone"
            projects.setdefault(project, 0)
            projects[project] += 1
            yield self._container(container, detail, labels, project)

        for project, count in projects.items():
            yield Entity(provider="docker", kind="compose_project", name=project, scope="local",
                         status="running", owner="platform", environment="dev",
                         tags=["compose"], attributes={"containers": count},
                         native_ids={"compose-project": project})

        yield from self._networks()
        yield from self._volumes()

    def _inspect(self, ident) -> dict:
        try:
            return json.loads(_docker("inspect", ident))[0]
        except (ValueError, IndexError):
            return {}

    def _container(self, container, detail, labels, project):
        state = (detail.get("State") or {})
        health = (state.get("Health") or {}).get("Status")
        running = state.get("Running")
        status = health or ("running" if running else "stopped")
        networks = list(((detail.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        mounts = detail.get("Mounts") or []

        edges = [CandidateEdge("CONTAINS", Reference(
            provider="docker", kind="compose_project", scope="local", name=project),
            rule="compose-label", confidence=EXPLICIT)]
        for network in networks:
            edges.append(CandidateEdge("DEPENDS_ON", Reference(
                provider="docker", kind="network", scope="local", name=network),
                rule="network-attachment", confidence=EXPLICIT, via="network"))
        for mount in mounts:
            if mount.get("Type") == "volume" and mount.get("Name"):
                edges.append(CandidateEdge("STORES_IN", Reference(
                    provider="docker", kind="volume", scope="local", name=mount["Name"]),
                    rule="mount", confidence=EXPLICIT, via=mount.get("Destination")))

        # A container whose name matches a service we also collect elsewhere is
        # very likely that service. Low confidence on purpose - it is a guess.
        service = _service_hint(container.get("Names", ""))
        if service:
            edges.append(CandidateEdge("EXPOSES", Reference(
                provider=service[0], kind=service[1], scope=service[2], name=service[3]),
                rule="name-convention", confidence=CONVENTIONAL))

        ports = [p for p in (container.get("Ports") or "").split(",") if "->" in p]
        return Entity(
            provider="docker", kind="container", name=container.get("Names", "?"), scope=project,
            status=status, owner="platform", environment="dev",
            tags=[t for t in [project, "healthy" if health == "healthy" else None] if t],
            attributes={"image": container.get("Image"), "state": container.get("State"),
                        "status_text": container.get("Status"), "ports": ports,
                        "networks": networks,
                        "mounts": [m.get("Name") or m.get("Source") for m in mounts][:10],
                        "started_at": state.get("StartedAt"),
                        "restart_count": state.get("RestartCount", 0)},
            native_ids={"docker-container": container.get("Names", ""),
                        "docker-id": container.get("ID", "")},
            edges=edges)

    def _networks(self):
        raw = _docker("network", "ls", "--format", "{{json .}}")
        for line in raw.splitlines():
            if not line.strip():
                continue
            net = json.loads(line)
            yield Entity(provider="docker", kind="network", name=net["Name"], scope="local",
                         status="active", owner="platform", environment="dev",
                         tags=[net.get("Driver", "")],
                         attributes={"driver": net.get("Driver"), "scope": net.get("Scope")},
                         native_ids={"docker-network": net["Name"]})

    def _volumes(self):
        raw = _docker("volume", "ls", "--format", "{{json .}}")
        for line in raw.splitlines():
            if not line.strip():
                continue
            vol = json.loads(line)
            yield Entity(provider="docker", kind="volume", name=vol["Name"], scope="local",
                         status="active", owner="platform", environment="dev",
                         tags=["persistent"],
                         attributes={"driver": vol.get("Driver"),
                                     "mountpoint": vol.get("Mountpoint")},
                         native_ids={"docker-volume": vol["Name"]})


# Containers whose name tells us which platform service they are.
_HINTS = [
    ("trino-marketplace", ("trino", "cluster", "default", "marketplace")),
    ("keycloak", ("keycloak", "realm", "nda", "nda")),
    ("minio", ("minio", "deployment", "default", "minio")),
    ("openmetadata-server", ("openmetadata", "service", "default", "openmetadata")),
]


def _service_hint(names: str):
    lowered = names.lower()
    for fragment, target in _HINTS:
        if fragment in lowered:
            return target
    return None


class KubernetesCollector:
    """Live API first; rendered manifests as a clearly-labelled fallback."""
    name = "kubernetes"
    provider = "kubernetes"
    kinds = ["cluster", "node", "namespace", "deployment", "statefulset",
             "service", "ingress", "persistentvolumeclaim", "configmap"]
    capabilities = {"inventory", "capacity", "dependencies"}

    MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "infra" / "k8s" / "generated.yaml"

    def health(self):
        if self._live():
            return {"ok": True, "detail": "live cluster"}
        if self.MANIFEST.exists():
            return {"ok": True, "detail": "manifests only (no cluster reachable)"}
        return {"ok": False, "detail": "no cluster and no manifests"}

    def _live(self) -> bool:
        try:
            result = subprocess.run(["kubectl", "get", "--raw", "/readyz"],
                                    capture_output=True, text=True, timeout=12,
                                    env={**os.environ, "MSYS_NO_PATHCONV": "1"})
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def collect(self):
        if self._live():
            yield from self._from_cluster()
        else:
            yield from self._from_manifests()

    def _from_cluster(self):
        yield Entity(provider="kubernetes", kind="cluster", name="default", scope="cluster",
                     status="ready", owner="platform", environment="prod", tags=["live"],
                     attributes={"source": "kubernetes-api"},
                     native_ids={"k8s-cluster": "default"})
        for kind, api in (("node", "nodes"), ("namespace", "namespaces"),
                          ("deployment", "deployments"), ("statefulset", "statefulsets"),
                          ("service", "services"), ("ingress", "ingresses"),
                          ("persistentvolumeclaim", "persistentvolumeclaims")):
            raw = subprocess.run(["kubectl", "get", api, "-A", "-o", "json"],
                                 capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "MSYS_NO_PATHCONV": "1"}).stdout
            try:
                items = json.loads(raw).get("items", [])
            except ValueError:
                continue
            for item in items:
                yield self._object(item, kind, observed=True)

    def _from_manifests(self):
        import yaml
        if not self.MANIFEST.exists():
            raise FileNotFoundError(f"no cluster reachable and no manifests at {self.MANIFEST}")
        documents = [d for d in yaml.safe_load_all(
            self.MANIFEST.read_text(encoding="utf-8")) if d]
        yield Entity(provider="kubernetes", kind="cluster", name="planned", scope="cluster",
                     status="declared", owner="platform", environment="prod",
                     tags=["manifest-only"],
                     attributes={"source": str(self.MANIFEST.name),
                                 "note": "rendered manifests, not a running cluster"},
                     native_ids={"k8s-cluster": "planned"})
        for document in documents:
            yield self._object(document, document["kind"].lower(), observed=False)

    def _object(self, item, kind, observed: bool):
        meta = item.get("metadata", {}) or {}
        namespace = meta.get("namespace") or "cluster"
        name = meta.get("name", "?")
        spec = item.get("spec", {}) or {}
        status = item.get("status", {}) or {}

        edges = []
        if namespace != "cluster":
            edges.append(CandidateEdge("CONTAINS", Reference(
                provider="kubernetes", kind="namespace", scope="cluster", name=namespace),
                rule="namespace", confidence=EXPLICIT))

        pod = (spec.get("template") or {}).get("spec") or {}
        for volume in pod.get("volumes", []) or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                edges.append(CandidateEdge("STORES_IN", Reference(
                    provider="kubernetes", kind="persistentvolumeclaim",
                    scope=namespace, name=claim), rule="volume", confidence=EXPLICIT))
        for dependency in (spec.get("template", {}).get("spec", {}) or {}).get("initContainers", []) or []:
            pass
        for upstream in (item.get("spec", {}).get("selector") or {}).get("matchLabels", {}).values():
            pass

        state = "ready"
        if observed:
            ready = status.get("readyReplicas")
            want = spec.get("replicas")
            if want is not None:
                state = "ready" if ready == want else f"{ready or 0}/{want}"
        else:
            state = "declared"

        containers = [c.get("image") for c in pod.get("containers", []) or []]
        return Entity(
            provider="kubernetes", kind=kind, name=name, scope=namespace,
            status=state, environment="prod",
            owner=(meta.get("labels") or {}).get("app.kubernetes.io/part-of") or "platform",
            tags=[t for t in [kind, "observed" if observed else "declared"] if t],
            attributes={"replicas": spec.get("replicas"), "images": containers,
                        "labels": meta.get("labels") or {},
                        "ports": [p.get("port") for p in spec.get("ports", []) or []],
                        "source": "kubernetes-api" if observed else "manifest"},
            native_ids={"k8s-object": f"{kind}/{namespace}/{name}",
                        **({"k8s-uid": meta["uid"]} if meta.get("uid") else {})},
            edges=edges)
