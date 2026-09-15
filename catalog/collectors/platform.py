"""The data platform: object store, query engine, catalogue, and who may read what.

These are the collectors that make the graph worth traversing. Terraform and
Kubernetes tell you what exists; these tell you what it MEANS - that a Trino
schema derives from another, that a catalog ultimately writes into one bucket,
and that a person can read it because a group they are in carries a role.

All read-only. MinIO is listed, never written; Trino is queried through
information_schema; Keycloak is read through the admin API.
"""
import json
import os
import pathlib
import subprocess
from typing import Iterable

from ..model import (CONVENTIONAL, DECLARED, EXPLICIT, CandidateEdge, Entity, Reference)

ROOT = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
class MinioCollector:
    name = "minio"
    provider = "minio"
    kinds = ["bucket", "deployment"]
    capabilities = {"inventory", "capacity"}

    def __init__(self, endpoint=None):
        self.endpoint = endpoint or os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")

    def _client(self):
        import boto3
        return boto3.client(
            "s3", endpoint_url=self.endpoint, region_name="us-east-1",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"))

    def health(self):
        try:
            self._client().list_buckets()
            return {"ok": True, "detail": self.endpoint}
        except Exception as error:                 # noqa: BLE001
            return {"ok": False, "detail": str(error)[:90]}

    def collect(self):
        client = self._client()
        buckets = client.list_buckets().get("Buckets", [])
        yield Entity(provider="minio", kind="deployment", name="minio", scope="default",
                     status="ok", owner="platform", environment="prod",
                     tags=["object-store"],
                     attributes={"endpoint": self.endpoint, "buckets": len(buckets)},
                     native_ids={"minio-deployment": "minio"})
        for bucket in buckets:
            name = bucket["Name"]
            objects, size = self._usage(client, name)
            yield Entity(
                provider="minio", kind="bucket", name=name, scope="default",
                status="ok", owner="platform", environment="prod", tags=["storage"],
                attributes={"created": str(bucket.get("CreationDate")),
                            "objects_sampled": objects, "bytes_sampled": size,
                            "uri": f"s3://{name}/"},
                # The id a Trino catalog property and a Terraform bucket both use.
                native_ids={"minio-bucket": name, "s3-uri": f"s3://{name}/"},
                edges=[CandidateEdge("CONTAINS", Reference(
                    provider="minio", kind="deployment", scope="default", name="minio"),
                    rule="deployment", confidence=EXPLICIT)])

    def _usage(self, client, bucket, cap=2000):
        """Sampled, not exhaustive - a catalog should not walk a data lake."""
        objects = size = 0
        token = None
        while objects < cap:
            kwargs = {"Bucket": bucket, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                objects += 1
                size += item.get("Size", 0)
            token = page.get("NextContinuationToken")
            if not token:
                break
        return objects, size


# --------------------------------------------------------------------------
class TrinoCollector:
    name = "trino"
    provider = "trino"
    kinds = ["cluster", "catalog", "schema", "table", "view"]
    capabilities = {"inventory", "lineage"}

    def _connect(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from marketplace.auth import connect_any
        return connect_any("marketplace_owner", catalog="iceberg")

    def health(self):
        try:
            conn = self._connect()
            conn.cursor().execute("SELECT 1")
            conn.close()
            return {"ok": True, "detail": "authenticated"}
        except Exception as error:                 # noqa: BLE001
            return {"ok": False, "detail": str(error).splitlines()[0][:90]}

    def collect(self):
        conn = self._connect()
        cursor = conn.cursor()
        try:
            yield Entity(provider="trino", kind="cluster", name="marketplace", scope="default",
                         status="ok", owner="platform", environment="prod",
                         tags=["query-engine", "governed"],
                         attributes={"auth": "keycloak-oidc", "tls": True},
                         native_ids={"trino-cluster": "marketplace"})

            cursor.execute("SHOW CATALOGS")
            catalogs = [r[0] for r in cursor.fetchall() if r[0] != "system"]
            for catalog in catalogs:
                # The bucket a catalog writes into is the join that proves the
                # conventional rule earns its place.
                warehouse = self._warehouse(cursor, catalog)
                edges = [CandidateEdge("CONTAINS", Reference(
                    provider="trino", kind="cluster", scope="default", name="marketplace"),
                    rule="cluster", confidence=EXPLICIT)]
                if warehouse:
                    edges.append(CandidateEdge("STORES_IN", Reference(
                        source="s3-uri", native_id=warehouse),
                        rule="catalog-warehouse-uri", confidence=CONVENTIONAL, via=warehouse))
                yield Entity(provider="trino", kind="catalog", name=catalog, scope="default",
                             status="ok", owner="platform", environment="prod",
                             tags=["catalog"], attributes={"warehouse": warehouse},
                             native_ids={"trino-catalog": catalog}, edges=edges)

                cursor.execute(f"SHOW SCHEMAS FROM {catalog}")
                for (schema,) in cursor.fetchall():
                    if schema == "information_schema":
                        continue
                    yield from self._schema(cursor, catalog, schema)
        finally:
            cursor.close()
            conn.close()

    def _warehouse(self, cursor, catalog):
        try:
            cursor.execute(
                "SELECT catalog_name, property_name, property_value "
                "FROM system.metadata.catalog_properties "
                f"WHERE catalog_name='{catalog}' AND property_name LIKE '%warehouse%'")
            rows = cursor.fetchall()
            return rows[0][2] if rows else None
        except Exception:                          # noqa: BLE001 - optional metadata
            return "s3://warehouse/" if catalog == "iceberg" else None

    def _schema(self, cursor, catalog, schema):
        kind = "schema"
        tags = ["schema"]
        if schema.startswith("sandbox_"):
            tags.append("sandbox")
        elif schema in ("nda_bronze", "nda_silver", "nda_gold"):
            tags.append("medallion")
        elif schema == "marketplace":
            tags.append("certified")

        yield Entity(provider="trino", kind=kind, name=f"{catalog}.{schema}", scope=catalog,
                     status="ok", environment="prod",
                     owner="data-engineering" if "nda_" in schema else "marketplace",
                     tags=tags, attributes={"catalog": catalog, "schema": schema},
                     native_ids={"trino-schema": f"{catalog}.{schema}"},
                     edges=[CandidateEdge("CONTAINS", Reference(
                         provider="trino", kind="catalog", scope="default", name=catalog),
                         rule="catalog", confidence=EXPLICIT)])

        try:
            cursor.execute(
                "SELECT table_name, table_type FROM "
                f"{catalog}.information_schema.tables WHERE table_schema='{schema}'")
            tables = cursor.fetchall()
        except Exception:                          # noqa: BLE001 - denied or empty
            return

        for name, table_type in tables[:400]:
            is_view = (table_type or "").upper().endswith("VIEW")
            edges = [CandidateEdge("CONTAINS", Reference(
                provider="trino", kind="schema", scope=catalog, name=f"{catalog}.{schema}"),
                rule="schema", confidence=EXPLICIT)]
            # Lineage: a certified view reads the gold layer underneath it.
            if is_view and schema == "marketplace":
                edges.append(CandidateEdge("DERIVES_FROM", Reference(
                    provider="trino", kind="schema", scope=catalog,
                    name=f"{catalog}.nda_gold"),
                    rule="marketplace-view-over-gold", confidence=DECLARED))
            yield Entity(provider="trino", kind="view" if is_view else "table",
                         name=f"{catalog}.{schema}.{name}", scope=f"{catalog}.{schema}",
                         status="ok", environment="prod",
                         owner="marketplace" if schema == "marketplace" else "data-engineering",
                         tags=["view" if is_view else "table", *tags],
                         attributes={"table_type": table_type},
                         native_ids={"trino-table": f"{catalog}.{schema}.{name}"},
                         edges=edges)


# --------------------------------------------------------------------------
class KeycloakCollector:
    """Users, groups, roles and clients - the ownership and access half."""
    name = "keycloak"
    provider = "keycloak"
    kinds = ["realm", "user", "group", "role", "client"]
    capabilities = {"inventory", "access", "ownership"}

    def _api(self, path):
        import sys
        sys.path.insert(0, str(ROOT))
        from marketplace.identity import api, token
        return api(path, self._token())

    def _token(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from marketplace.identity import token
        if not hasattr(self, "_cached"):
            self._cached = token()
        return self._cached

    def health(self):
        try:
            self._api("/groups?max=1")
            return {"ok": True, "detail": os.environ.get("KEYCLOAK_URL", "keycloak")}
        except Exception as error:                 # noqa: BLE001
            return {"ok": False, "detail": str(error).splitlines()[0][:90]}

    def collect(self):
        realm = os.environ.get("KEYCLOAK_REALM", "nda")
        yield Entity(provider="keycloak", kind="realm", name=realm, scope="nda",
                     status="ok", owner="platform", environment="prod",
                     tags=["identity", "sso"], attributes={"realm": realm},
                     native_ids={"keycloak-realm": realm})

        groups = self._api("/groups?max=200")
        group_roles = {}
        for group in groups:
            detail = self._api(f"/groups/{group['id']}")
            attributes = detail.get("attributes") or {}
            trino_role = (attributes.get("trino_role") or [None])[0]
            group_roles[group["name"]] = trino_role
            edges = [CandidateEdge("CONTAINS", Reference(
                provider="keycloak", kind="realm", scope="nda", name=realm),
                rule="realm", confidence=EXPLICIT)]
            if trino_role:
                # The grant that connects identity to data access.
                edges.append(CandidateEdge("GRANTS", Reference(
                    provider="keycloak", kind="role", scope="nda", name=trino_role),
                    rule="group-attribute:trino_role", confidence=EXPLICIT,
                    via="trino_role"))
            yield Entity(provider="keycloak", kind="group", name=group["name"], scope="nda",
                         status="ok", owner="platform", environment="prod",
                         tags=["group", *( [trino_role] if trino_role else [])],
                         attributes={"path": group.get("path"), "trino_role": trino_role,
                                     "description": (attributes.get("description") or [""])[0]},
                         native_ids={"keycloak-group": group["name"]}, edges=edges)

        for role in {r for r in group_roles.values() if r}:
            yield Entity(provider="keycloak", kind="role", name=role, scope="nda",
                         status="ok", owner="platform", environment="prod",
                         tags=["role", "warehouse"],
                         attributes={"enforced_by": "trino file access control"},
                         native_ids={"keycloak-role": role, "trino-role": role})

        for group in groups:
            for member in self._api(f"/groups/{group['id']}/members?max=200"):
                yield Entity(
                    provider="keycloak", kind="user", name=member["username"], scope="nda",
                    status="enabled" if member.get("enabled", True) else "disabled",
                    owner=group["name"], environment="prod",
                    tags=["user", group["name"]],
                    attributes={"email": member.get("email"),
                                "name": f"{member.get('firstName','')} {member.get('lastName','')}".strip()},
                    native_ids={"keycloak-user": member["username"]},
                    edges=[CandidateEdge("MEMBER_OF", Reference(
                        provider="keycloak", kind="group", scope="nda", name=group["name"]),
                        rule="group-membership", confidence=EXPLICIT)])

        for client in self._api("/clients?max=100"):
            yield Entity(provider="keycloak", kind="client", name=client["clientId"], scope="nda",
                         status="enabled" if client.get("enabled") else "disabled",
                         owner="platform", environment="prod", tags=["oidc-client"],
                         attributes={"public": client.get("publicClient"),
                                     "flows": [k for k, v in client.items()
                                               if k.endswith("Enabled") and v is True][:6]},
                         native_ids={"keycloak-client": client["clientId"]},
                         edges=[CandidateEdge("CONTAINS", Reference(
                             provider="keycloak", kind="realm", scope="nda", name=realm),
                             rule="realm", confidence=EXPLICIT)])


# --------------------------------------------------------------------------
class MarketplaceCollector:
    """The governed data products, their owners, and the rules over them.

    This is the one collector that reads a declaration rather than a running
    system, and it is where ownership actually comes from: products.py names an
    owner for every certified product and an audience for every role.
    """
    name = "marketplace"
    provider = "marketplace"
    kinds = ["data_product", "access_rule"]
    capabilities = {"inventory", "ownership", "access"}

    def health(self):
        return {"ok": (ROOT / "marketplace" / "products.py").exists(),
                "detail": "products.py"}

    def collect(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from marketplace.products import PRODUCTS, PRODUCT_AUDIENCE, ROLES

        for product in PRODUCTS:
            audience = PRODUCT_AUDIENCE.get(product.name, [])
            edges = [CandidateEdge("DERIVES_FROM", Reference(
                source="trino-table", native_id=f"iceberg.marketplace.{product.name}"),
                rule="product-view", confidence=EXPLICIT)]
            for source in getattr(product, "sources", []):
                edges.append(CandidateEdge("DEPENDS_ON", Reference(
                    source="trino-table", native_id=source),
                    rule="declared-source", confidence=EXPLICIT))
            for role in audience:
                edges.append(CandidateEdge("GRANTS", Reference(
                    provider="keycloak", kind="role", scope="nda", name=role),
                    rule="product-audience", confidence=EXPLICIT, via="SELECT"))
            yield Entity(
                provider="marketplace", kind="data_product", name=product.name,
                scope="marketplace", status="certified",
                owner=product.owner, environment="prod",
                tags=["certified", getattr(product, "domain", "data")],
                attributes={"title": product.title,
                            "description": product.description[:400],
                            "audience": audience,
                            "measures": [m.name for m in product.measures],
                            "dimensions": [d.name for d in product.dimensions],
                            "sources": list(getattr(product, "sources", []))},
                native_ids={"marketplace-product": product.name},
                edges=edges)

        for role in ROLES:
            products = [p.name for p in PRODUCTS if role in PRODUCT_AUDIENCE.get(p.name, [])]
            yield Entity(
                provider="marketplace", kind="access_rule", name=f"role:{role}",
                scope="marketplace", status="enforced", owner="platform", environment="prod",
                tags=["rbac", role],
                attributes={"role": role, "products": products,
                            "enforced_by": "trino file-based access control"},
                native_ids={"marketplace-rule": role})
