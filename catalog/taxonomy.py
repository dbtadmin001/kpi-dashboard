"""What things are called for people, and how they group.

The collectors speak their providers' languages - `statefulset`, `tfstate`,
`preferred_username`. That vocabulary is correct and it is also unreadable to
anyone who does not already run the thing. This file is the one place that
translates, so the UI never has to and the words stay consistent across every
view.

Two ideas:

  GROUPS    what a cloud console calls a resource group - a small set of
            categories a person can hold in their head, assigned from what a
            resource IS rather than which tool reported it. Someone looking for
            "our databases" should not need to know that Trino reported the
            schema and Terraform reported the server.

  LABELS    the noun a person would use. `statefulset` becomes "Stateful
            service"; `MEMBER_OF` becomes "belongs to".
"""

# Resource groups, in the order a console should list them. `match` is
# (provider, kind) where "*" means any.
GROUPS = [
    ("compute", "Compute", "Containers, clusters and the machines they run on", [
        ("docker", "container"), ("kubernetes", "deployment"),
        ("kubernetes", "statefulset"), ("kubernetes", "node"),
        ("kubernetes", "cluster"), ("kubernetes", "daemonset"),
    ]),
    ("storage", "Storage", "Buckets, volumes and persistent disks", [
        ("minio", "*"), ("docker", "volume"),
        ("kubernetes", "persistentvolumeclaim"), ("kubernetes", "persistentvolume"),
        ("kubernetes", "storageclass"),
    ]),
    ("data", "Data & analytics", "Catalogs, schemas, tables and the query engine", [
        ("trino", "*"), ("openmetadata", "*"),
    ]),
    ("products", "Data products", "Certified datasets people build reports on", [
        ("marketplace", "data_product"),
    ]),
    ("identity", "Identity & access", "People, teams, roles and the rules over them", [
        ("keycloak", "*"), ("marketplace", "access_rule"),
    ]),
    ("networking", "Networking", "Networks, services and how traffic reaches them", [
        ("docker", "network"), ("kubernetes", "service"), ("kubernetes", "ingress"),
    ]),
    ("pipelines", "Pipelines", "Scheduled jobs and the streams that feed them", [
        ("airflow", "*"), ("kafka", "*"),
    ]),
    ("iac", "Infrastructure as code", "What Terraform declares and manages", [
        ("terraform", "*"),
    ]),
    ("config", "Configuration", "Settings, manifests and generated files", [
        ("kubernetes", "configmap"), ("kubernetes", "poddisruptionbudget"),
        ("kubernetes", "namespace"), ("docker", "compose_project"),
        ("file", "*"),
    ]),
]

GROUP_LABEL = {key: label for key, label, _, _ in GROUPS}
GROUP_ABOUT = {key: about for key, _, about, _ in GROUPS}


def group_for(provider: str, kind: str) -> str:
    """Which resource group something belongs to. Exact match beats wildcard."""
    for key, _, _, members in GROUPS:
        if (provider, kind) in members:
            return key
    for key, _, _, members in GROUPS:
        if (provider, "*") in members:
            return key
    return "other"


# The noun a person would use, per resource kind.
LABELS = {
    "container": "Container", "compose_project": "Application", "network": "Network",
    "volume": "Disk", "image": "Image",
    "cluster": "Cluster", "node": "Machine", "namespace": "Environment",
    "deployment": "Service", "statefulset": "Stateful service",
    "service": "Network endpoint", "ingress": "Public endpoint",
    "persistentvolumeclaim": "Disk request", "storageclass": "Disk type",
    "configmap": "Configuration", "poddisruptionbudget": "Availability rule",
    "workspace": "Terraform workspace", "state_resource": "Managed resource",
    "module": "Module",
    "bucket": "Bucket", "deployment_minio": "Object storage",
    "catalog": "Data catalog", "schema": "Database", "table": "Table", "view": "View",
    "data_product": "Data product", "access_rule": "Access rule",
    "realm": "Directory", "user": "Person", "group": "Team", "role": "Role",
    "client": "Application registration",
    "dag": "Scheduled job", "pipeline": "Pipeline",
    "om_service": "Connected source", "om_database": "Catalogued database",
}

# Where a resource came from, said plainly.
SOURCES = {
    "terraform": "Terraform", "docker": "Docker", "kubernetes": "Kubernetes",
    "minio": "Object storage", "trino": "Query engine", "keycloak": "Directory",
    "marketplace": "Data marketplace", "openmetadata": "Data catalog",
    "airflow": "Scheduler", "file": "Files",
}

# Relationships, as a sentence fragment that reads left to right:
#   "<this>  needs  <that>"
EDGE_LABELS = {
    "MANAGES": "creates and manages",
    "CONTAINS": "is part of",
    "DEPENDS_ON": "needs",
    "EXPOSES": "serves",
    "STORES_IN": "stores data in",
    "DERIVES_FROM": "is built from",
    "GRANTS": "gives access to",
    "MEMBER_OF": "belongs to",
}

# Confidence, in words. A number between 0 and 1 means nothing to a reader.
def confidence_label(value):
    if value is None or value >= 1.0:
        return ("Confirmed", "Both systems report the same identifier")
    if value >= 0.85:
        return ("Declared", "One system's configuration names the other")
    return ("Likely", "Matched by a naming convention - worth verifying")


def label_for(kind: str, provider: str = None) -> str:
    if provider == "minio" and kind == "deployment":
        return LABELS["deployment_minio"]
    return LABELS.get(kind, kind.replace("_", " ").capitalize())


def source_for(provider: str) -> str:
    return SOURCES.get(provider, provider.capitalize())
