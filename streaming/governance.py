"""GDPR controls for the NDA lakehouse: classification, masking, retention, erasure.

Classification is code, not a wiki page, so that every control below is derived
from one declaration and drift is detectable (see audit()).

Which GDPR articles each control implements:

  Art. 5(1)(c)  data minimisation   - the CDC projection carries no applicant
                                      names or contact details at all; the only
                                      identity in the fact stream is a
                                      pseudonymous entity_id. audit() fails if an
                                      unclassified column appears.
  Art. 5(1)(e)  storage limitation  - RETENTION gives every layer a retention
                                      period; expire() enforces it by expiring
                                      Iceberg snapshots so deleted rows stop
                                      being reachable through time travel.
  Art. 15       right of access     - subject_access() assembles everything held
                                      about one entity across all layers.
  Art. 17       right to erasure    - erase() deletes at the SOURCE first, lets
                                      CDC carry the delete through bronze/silver/
                                      gold, then expires snapshots so the files
                                      are actually unreachable. Deleting only in
                                      the lake would be undone by the next CDC
                                      snapshot, which is the classic mistake.
  Art. 25       protection by design- masking_sql() ships a masked view so the
                                      default grant for analysts never exposes
                                      direct identifiers.
  Art. 30       records of processing- processing_record() emits the Art. 30
                                      register from the same declaration.
  Art. 32       security            - pseudonymisation at source; direct
                                      identifiers live only in the enrichment
                                      dimension, which is separately erasable.

A caveat that must not be lost: Iceberg time travel means a logical DELETE is not
an erasure until snapshots expire. erase() therefore is not complete until
expire() has run over every affected table; erase() reports that explicitly.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from .contracts import COLUMNS, PROCESSES, table_names

# category: what the column is; basis: GDPR lawful basis; pii: needs masking.
CLASSIFICATION = {
    "record_id": ("indirect_identifier", "legal_obligation", False),
    "application_id": ("indirect_identifier", "legal_obligation", False),
    "entity_id": ("pseudonymous_identifier", "legal_obligation", True),
    "process_code": ("operational", "legal_obligation", False),
    "application_type": ("operational", "legal_obligation", False),
    "activity_type": ("operational", "legal_obligation", False),
    "route": ("operational", "legal_obligation", False),
    "cohort_month": ("operational", "legal_obligation", False),
    "received_at": ("operational", "legal_obligation", False),
    "due_at": ("operational", "legal_obligation", False),
    "completed_at": ("operational", "legal_obligation", False),
    "updated_at": ("operational", "legal_obligation", False),
    "status": ("operational", "legal_obligation", False),
    "outcome": ("regulatory_decision", "legal_obligation", False),
    "touch_days": ("operational", "legal_obligation", False),
    "wait_days": ("operational", "legal_obligation", False),
    "revision": ("technical", "legal_obligation", False),
}
# The enrichment dimension is where direct identifiers actually live.
ENRICHMENT_CLASSIFICATION = {
    "entity_id": ("pseudonymous_identifier", "legal_obligation", True),
    "legal_name": ("direct_identifier", "legitimate_interest", True),
    "country": ("location", "legitimate_interest", True),
    "risk_tier": ("derived_assessment", "legitimate_interest", False),
    "registry_status": ("derived_assessment", "legitimate_interest", False),
    "enriched_at": ("technical", "legitimate_interest", False),
    "source_latency_ms": ("technical", "legitimate_interest", False),
    "status": ("technical", "legitimate_interest", False),
}
# Storage limitation per layer, in days.
RETENTION = {"nda_bronze": 30, "nda_silver": 365 * 2, "nda_gold": 365 * 7}
SUBJECT_TABLES = [f"nda_gold.fact_{t}" for t in table_names()] + \
                 [f"nda_silver.{t}" for t in table_names()] + \
                 ["nda_gold.dim_entity_enrichment"]


def pii_columns(scope="fact"):
    source = CLASSIFICATION if scope == "fact" else ENRICHMENT_CLASSIFICATION
    return [c for c, (_, _, pii) in source.items() if pii]


def masking_sql():
    """Art. 25: the default analyst-facing view never exposes direct identifiers."""
    masked = []
    for column in COLUMNS:
        if CLASSIFICATION[column][2]:
            # Keep joinability for aggregate analysis without exposing the identifier.
            masked.append(f"'sha256:' || TO_HEX(SHA256(CAST({column} AS VARBINARY))) AS {column}")
        else:
            masked.append(column)
    facts = " UNION ALL ".join(f"SELECT {','.join(masked)} FROM iceberg.nda_gold.fact_{p.lower()}_applications"
                              for p in PROCESSES)
    enrichment = ("SELECT 'sha256:' || TO_HEX(SHA256(CAST(entity_id AS VARBINARY))) AS entity_id,"
                  " CAST(NULL AS VARCHAR) AS legal_name, CAST(NULL AS VARCHAR) AS country,"
                  " risk_tier, registry_status, enriched_at, status"
                  " FROM iceberg.nda_gold.dim_entity_enrichment")
    return [f"CREATE OR REPLACE VIEW iceberg.nda_gold.applications_masked AS {facts}",
            f"CREATE OR REPLACE VIEW iceberg.nda_gold.entity_enrichment_masked AS {enrichment}"]


def processing_record():
    """Art. 30 register, derived from the same declaration the pipeline uses."""
    return {
        "controller": "National Drug Authority (synthetic demonstration)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purposes": ["Regulatory performance monitoring of MA, CT and GMP processes"],
        "categories_of_data": sorted({c for c, _, _ in
                                      list(CLASSIFICATION.values()) + list(ENRICHMENT_CLASSIFICATION.values())}),
        "lawful_bases": sorted({b for _, b, _ in
                                list(CLASSIFICATION.values()) + list(ENRICHMENT_CLASSIFICATION.values())}),
        "recipients": ["Internal regulatory performance reporting"],
        "retention_days": RETENTION,
        "transfers_outside_eea": "None; single-region deployment",
        "security_measures": ["pseudonymous identifiers at source", "masked default views",
                              "separate CDC principal with read-only access",
                              "snapshot expiry enforcing erasure"],
        "pii_columns": {"facts": pii_columns("fact"), "enrichment": pii_columns("enrichment")},
    }


def audit(conn):
    """Fail loudly when a column reaches the lake without a classification."""
    issues = []
    for column in COLUMNS:
        if column not in CLASSIFICATION:
            issues.append({"kind": "unclassified_column", "column": column})
    cursor = conn.cursor()
    cursor.execute("SELECT column_name FROM iceberg.information_schema.columns "
                   "WHERE table_schema='nda_gold' AND table_name='dim_entity_enrichment'")
    for (column,) in cursor.fetchall():
        if column not in ENRICHMENT_CLASSIFICATION:
            issues.append({"kind": "unclassified_enrichment_column", "column": column})
    for layer, days in RETENTION.items():
        if days <= 0:
            issues.append({"kind": "invalid_retention", "layer": layer})
    return {"classified_fact_columns": len(CLASSIFICATION), "pii_fact_columns": pii_columns("fact"),
            "pii_enrichment_columns": pii_columns("enrichment"), "retention_days": RETENTION,
            "issues": issues}


def subject_access(conn, entity_id):
    """Art. 15: everything the platform holds about one entity."""
    held = {}
    cursor = conn.cursor()
    for table in SUBJECT_TABLES:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM iceberg.{table} WHERE entity_id = '{entity_id}'")
            count = cursor.fetchone()[0]
        except Exception:
            continue
        if count:
            held[table] = count
    return held


def erasure_connection():
    """Least privilege: only this principal may DELETE regulatory records."""
    import pymssql
    return pymssql.connect(server=os.environ.get("SQLSERVER_HOST", "127.0.0.1"),
                           port=int(os.environ.get("SQLSERVER_PORT", "14333")),
                           user="nda_erasure", password=os.environ["ERASURE_PASSWORD"],
                           database=os.environ.get("SQLSERVER_DATABASE", "NDAStreaming"),
                           login_timeout=10, timeout=60, autocommit=False)


def erase(conn, sql_conn, entity_id, expire_after=True):
    """Art. 17. Source first, then the lake, then make time travel forget it."""
    removed = {"source_rows": 0, "lake_rows": 0, "tables": []}
    with sql_conn.cursor() as cursor:
        # Collect the ids BEFORE deleting: once the applications are gone there is
        # nothing left to resolve the registry rows from, and the subject's id would
        # survive erasure as a residual trace.
        claimed = []
        for process in PROCESSES:
            cursor.execute(f"SELECT record_id FROM dbo.{process.lower()}_applications WHERE entity_id=%s", (entity_id,))
            claimed.extend(row[0] for row in cursor.fetchall())
        # Children before parents; the FK is what makes the order mandatory.
        for family in ("steps", "activities", "applications"):
            for process in PROCESSES:
                table = f"{process.lower()}_{family}"
                cursor.execute(f"DELETE FROM dbo.{table} WHERE entity_id=%s", (entity_id,))
                if cursor.rowcount > 0:
                    removed["source_rows"] += cursor.rowcount
                    removed["tables"].append(f"sqlserver.dbo.{table}")
        for application_id in claimed:
            cursor.execute("DELETE FROM dbo.application_registry WHERE application_id=%s", (application_id,))
            removed["source_rows"] += cursor.rowcount
        removed["registry_ids_released"] = len(claimed)
    sql_conn.commit()
    # The enrichment dimension is not CDC-fed, so it must be erased directly.
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM iceberg.nda_gold.dim_entity_enrichment WHERE entity_id = '{entity_id}'")
    cursor.fetchall()
    removed["tables"].append("iceberg.nda_gold.dim_entity_enrichment")
    if expire_after:
        removed["expired"] = expire(conn, only=["nda_gold.dim_entity_enrichment"])
    removed["note"] = ("CDC carries the source deletes through bronze/silver/gold; run expire() "
                       "over the affected tables afterwards or the rows stay reachable via time travel")
    return removed


def expire(conn, only=None, now=None):
    """Art. 5(1)(e): drop snapshots past retention so deleted files become unreachable."""
    now = now or datetime.now(timezone.utc)
    cursor = conn.cursor()
    expired = []
    targets = only or [f"{layer}.{name}" for layer in RETENTION
                       for name in _tables_in(conn, layer)]
    for table in targets:
        layer = table.split(".")[0]
        cutoff = now - timedelta(days=RETENTION.get(layer, 30))
        try:
            cursor.execute(f"ALTER TABLE iceberg.{table} EXECUTE expire_snapshots("
                           f"retention_threshold => '{max((now - cutoff).days, 7)}d')")
            cursor.fetchall()
            expired.append(table)
        except Exception as error:
            expired.append(f"{table} (skipped: {str(error)[:60]})")
    return expired


def _tables_in(conn, schema):
    cursor = conn.cursor()
    cursor.execute(f"SELECT table_name FROM iceberg.information_schema.tables "
                   f"WHERE table_schema='{schema}' AND table_type='BASE TABLE'")
    return [r[0] for r in cursor.fetchall()]


# --- OpenMetadata classification --------------------------------------------
# The same declaration, pushed into the catalog so the governance story is the
# one the business reads in the UI, not a second copy that drifts from the code.
# OpenMetadata ships PII and PersonalData classifications out of the box.
OM_TAGS = {
    "pseudonymous_identifier": ["PII.Sensitive", "PersonalData.Personal"],
    "direct_identifier": ["PII.Sensitive", "PersonalData.Personal"],
    "location": ["PII.NonSensitive", "PersonalData.Personal"],
    "indirect_identifier": ["PII.NonSensitive"],
    "regulatory_decision": ["PII.NonSensitive"],
}
LAYER_TAGS = {"nda_bronze": "DataLayer.Bronze", "nda_silver": "DataLayer.Silver", "nda_gold": "DataLayer.Gold"}


def om_targets():
    """(fullyQualifiedName, {column: category}) for every table the catalog holds."""
    for table in table_names():
        yield f"nda_sqlserver.NDAStreaming.dbo.{table}", {c: v[0] for c, v in CLASSIFICATION.items()}
        for layer in ("nda_silver", "nda_gold"):
            name = table if layer == "nda_silver" else f"fact_{table}"
            yield f"nda_trino.iceberg.{layer}.{name}", {c: v[0] for c, v in CLASSIFICATION.items()}
    yield "nda_trino.iceberg.nda_gold.dim_entity_enrichment", {c: v[0] for c, v in ENRICHMENT_CLASSIFICATION.items()}


def classify_openmetadata(url=None, token=None):
    """Tag every classified column in the catalog; returns (tables, columns) touched."""
    import requests
    from urllib.parse import quote
    base = (url or os.environ["OPENMETADATA_URL"]).rstrip("/")
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + (token or os.environ["OPENMETADATA_TOKEN"])
    tables = columns = 0
    for fqn, mapping in om_targets():
        response = session.get(f"{base}/v1/tables/name/{quote(fqn, safe='')}?fields=columns,tags", timeout=30)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        table = response.json()
        patch, touched = [], 0
        for index, column in enumerate(table.get("columns", [])):
            labels = OM_TAGS.get(mapping.get(column["name"]), [])
            if not labels:
                continue
            existing = {t["tagFQN"] for t in column.get("tags", [])}
            new = [{"tagFQN": t, "source": "Classification", "labelType": "Manual", "state": "Confirmed"}
                   for t in labels if t not in existing]
            if new:
                patch.append({"op": "add", "path": f"/columns/{index}/tags",
                              "value": list(column.get("tags", [])) + new})
                touched += 1
        layer = fqn.split(".")[2] if fqn.startswith("nda_trino") else None
        if layer in LAYER_TAGS and not any(t["tagFQN"] == LAYER_TAGS[layer] for t in table.get("tags", [])):
            patch.append({"op": "add", "path": "/tags",
                          "value": list(table.get("tags", [])) + [
                              {"tagFQN": LAYER_TAGS[layer], "source": "Classification",
                               "labelType": "Manual", "state": "Confirmed"}]})
        if patch:
            applied = session.patch(f"{base}/v1/tables/{table['id']}", json=patch, timeout=30,
                                    headers={"Content-Type": "application/json-patch+json"})
            applied.raise_for_status()
            tables += 1
            columns += touched
    print(f"Classified {columns} columns across {tables} catalog tables")
    return tables, columns


def main():
    parser = argparse.ArgumentParser(description="GDPR controls for the NDA lakehouse")
    parser.add_argument("action", choices=["audit", "mask", "record", "access", "erase", "expire", "classify"])
    parser.add_argument("--entity-id", default=None)
    parser.add_argument("--confirm", action="store_true", help="Required for erase")
    args = parser.parse_args()
    from .enrichment import connection
    conn = connection()
    try:
        if args.action == "classify":
            classify_openmetadata()
        elif args.action == "audit":
            print(json.dumps(audit(conn), indent=2))
        elif args.action == "mask":
            cursor = conn.cursor()
            for statement in masking_sql():
                cursor.execute(statement); cursor.fetchall()
            print(f"Applied {len(masking_sql())} masked views")
        elif args.action == "record":
            print(json.dumps(processing_record(), indent=2))
        elif args.action == "access":
            if not args.entity_id:
                raise SystemExit("--entity-id is required")
            print(json.dumps(subject_access(conn, args.entity_id), indent=2))
        elif args.action == "erase":
            if not args.entity_id or not args.confirm:
                raise SystemExit("--entity-id and --confirm are required; erasure is irreversible")
            sql_conn = erasure_connection()
            try:
                print(json.dumps(erase(conn, sql_conn, args.entity_id), indent=2))
            finally:
                sql_conn.close()
        elif args.action == "expire":
            print(json.dumps(expire(conn), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
