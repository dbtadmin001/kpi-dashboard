"""Terraform / OpenTofu state: what the code declares.

State is read, never written, and never locked - this opens the file (or the S3
object) for reading and nothing else. Terraform stays the source of truth; the
catalog only ever reports on it.

Two things make this collector the most valuable one:

  * `dependencies` on each instance is a dependency graph Terraform already
    computed. Nothing else in the estate knows it.
  * Provider attributes frequently NAME the runtime object - a keycloak_group
    has its group name, a kubernetes_deployment has metadata.name and namespace.
    That is the DECLARED rule, and in practice it carries most of the join.
"""
import json
import pathlib
from typing import Iterable, List

from ..model import CONVENTIONAL, DECLARED, CandidateEdge, Entity, Reference

# Attribute names a resource uses to point at its runtime counterpart, in the
# order we trust them.
NAME_KEYS = ("name", "bucket", "client_id", "username", "realm", "metadata.0.name")


class TerraformCollector:
    name = "terraform"
    provider = "terraform"
    kinds = ["workspace", "state_resource", "module"]
    capabilities = {"inventory", "dependencies", "ownership"}

    def __init__(self, paths: Iterable[str] = None, root: pathlib.Path = None):
        self.root = pathlib.Path(root or pathlib.Path(__file__).resolve().parents[2])
        self.paths = [pathlib.Path(p) for p in paths] if paths else self._discover()

    def _discover(self) -> List[pathlib.Path]:
        found = []
        for pattern in ("*.tfstate", "**/*.tfstate", "**/terraform.tfstate"):
            found += [p for p in self.root.glob(pattern)
                      if ".terraform" not in p.parts and "backup" not in p.name]
        return sorted(set(found))

    def health(self):
        return {"ok": bool(self.paths),
                "detail": f"{len(self.paths)} state file(s)" if self.paths
                          else "no .tfstate found"}

    def collect(self):
        for path in self.paths:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            workspace = path.parent.name
            resources = state.get("resources", [])

            yield Entity(
                provider="terraform", kind="workspace", name=workspace, scope="iac",
                status="ok", environment="control-plane", owner="platform",
                tags=["iac", "terraform"],
                attributes={"path": str(path.relative_to(self.root)),
                            "serial": state.get("serial"),
                            "terraform_version": state.get("terraform_version"),
                            "resource_count": len(resources)},
                native_ids={"terraform-workspace": workspace})

            for resource in resources:
                yield from self._resource(resource, workspace)

    def _resource(self, resource, workspace):
        rtype = resource.get("type", "unknown")
        rname = resource.get("name", "unnamed")
        address = f"{rtype}.{rname}"
        mode = resource.get("mode", "managed")

        for index, instance in enumerate(resource.get("instances", [])):
            attributes = instance.get("attributes", {}) or {}
            key = instance.get("index_key")
            label = f"{address}[{key}]" if key is not None else address

            edges = [CandidateEdge("CONTAINS", Reference(
                provider="terraform", kind="workspace", scope="iac", name=workspace),
                rule="state-file", confidence=1.0)]

            # Terraform already computed this graph. Reuse it rather than guess.
            for dependency in instance.get("dependencies", []) or []:
                edges.append(CandidateEdge(
                    "DEPENDS_ON",
                    Reference(source="tf-address", native_id=dependency),
                    rule="tfstate-dependencies", confidence=1.0))

            # The DECLARED rule: the provider's own attributes name the thing
            # that will exist at runtime.
            for target in self._runtime_targets(rtype, attributes):
                edges.append(target)

            yield Entity(
                provider="terraform", kind="state_resource", name=label, scope=workspace,
                status="managed" if mode == "managed" else mode,
                owner="platform", environment="control-plane",
                tags=[rtype.split("_")[0], mode],
                attributes={"type": rtype, "address": label, "provider": resource.get("provider"),
                            "mode": mode, "index": key,
                            "attributes": _safe(attributes)},
                native_ids={"tf-address": label,
                            **({"tf-id": str(attributes["id"])} if attributes.get("id") else {})},
                edges=edges)

    def _runtime_targets(self, rtype, attributes) -> List[CandidateEdge]:
        """Point a state resource at the live object it manages, when it says so."""
        out = []

        def manage(provider, kind, name, scope="default", rule="declared", conf=DECLARED):
            if name:
                out.append(CandidateEdge("MANAGES", Reference(
                    provider=provider, kind=kind, scope=scope, name=str(name)),
                    rule=rule, confidence=conf))

        if rtype.startswith("keycloak_"):
            realm = attributes.get("realm_id") or attributes.get("realm") or "nda"
            if rtype == "keycloak_group":
                manage("keycloak", "group", attributes.get("name"), "nda")
            elif rtype == "keycloak_user":
                manage("keycloak", "user", attributes.get("username"), "nda")
            elif rtype == "keycloak_openid_client":
                manage("keycloak", "client", attributes.get("client_id"), "nda")
            elif rtype == "keycloak_role":
                manage("keycloak", "role", attributes.get("name"), "nda")
            elif rtype == "keycloak_realm":
                manage("keycloak", "realm", attributes.get("realm"), "nda")
        elif rtype.startswith("kubernetes_"):
            meta = (attributes.get("metadata") or [{}])
            meta = meta[0] if isinstance(meta, list) and meta else {}
            kind = rtype.replace("kubernetes_", "").replace("_v1", "")
            manage("kubernetes", kind, meta.get("name"), meta.get("namespace", "default"))
        elif "bucket" in rtype:
            manage("minio", "bucket", attributes.get("bucket") or attributes.get("name"))
        elif rtype in ("local_file", "local_sensitive_file"):
            filename = attributes.get("filename")
            if filename:
                out.append(CandidateEdge("MANAGES", Reference(
                    provider="file", kind="config", scope="generated",
                    name=pathlib.Path(str(filename)).name),
                    rule="declared-filename", confidence=CONVENTIONAL))
        return out


def _safe(attributes: dict) -> dict:
    """Keep the payload, minus anything that looks like a credential.

    The catalog records that a secret EXISTS and what references it; it is not a
    place to read one from.
    """
    redacted = {}
    for key, value in attributes.items():
        lowered = key.lower()
        if any(t in lowered for t in ("password", "secret", "token", "private_key", "credential")):
            redacted[key] = "***redacted***"
        elif isinstance(value, (dict, list)):
            redacted[key] = json.loads(json.dumps(value, default=str))[:20] \
                if isinstance(value, list) else json.loads(json.dumps(value, default=str))
        else:
            redacted[key] = value
    return redacted
