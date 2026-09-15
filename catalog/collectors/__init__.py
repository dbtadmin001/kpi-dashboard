"""Collector registry.

Adding a provider is one entry here plus one class. Nothing in the core knows
what a Trino catalog or a Docker volume is - collectors emit Entities, the
resolver decides identity, and the store keeps whatever arrives.
"""
from .platform import KeycloakCollector, MarketplaceCollector, MinioCollector, TrinoCollector
from .runtime import DockerCollector, KubernetesCollector
from .terraform import TerraformCollector

# Order matters only for readability; the resolver links edges after every
# collector has run, so a collector may reference something reported later.
REGISTRY = {
    "terraform": TerraformCollector,
    "docker": DockerCollector,
    "kubernetes": KubernetesCollector,
    "minio": MinioCollector,
    "trino": TrinoCollector,
    "keycloak": KeycloakCollector,
    "marketplace": MarketplaceCollector,
}


def all_collectors(names=None):
    chosen = names or list(REGISTRY)
    return [REGISTRY[n]() for n in chosen if n in REGISTRY]


def health():
    out = []
    for name, factory in REGISTRY.items():
        try:
            out.append({"plugin": name, **factory().health()})
        except Exception as error:                 # noqa: BLE001
            out.append({"plugin": name, "ok": False, "detail": str(error)[:90]})
    return out
