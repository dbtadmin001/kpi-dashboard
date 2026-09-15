"""Lineage for the NDA pipeline, emitted as OpenLineage events.

OpenLineage is the vendor-neutral standard (Marquez is its reference server), so
the same events also load into OpenMetadata, DataHub or Atlan without rework.

Datasets are named by the OpenLineage naming convention, which is what makes a
graph traversable: a dataset produced by one job and consumed by the next must
carry byte-identical namespace/name in both events, or the graph fragments into
disconnected islands. Every hop of the pipeline is therefore derived from one
contract (streaming.contracts) rather than hand-listed.

Emitted graph (each arrow is a job, so lineage traverses both directions):

  sqlserver.dbo.<table>                       -- Debezium CDC
    -> kafka nda.NDAStreaming.dbo.<table>     -- Flink bronze / silver
      -> iceberg nda_bronze.<table>
      -> iceberg nda_silver.<table>           (+ quarantine_<table>)
        -> iceberg nda_gold.fact_<table>      -- Flink gold
        -> iceberg nda_gold.fact_<p>_kpi_measurements
          -> iceberg nda_gold.kpi_quarterly   -- Trino view
  iceberg nda_gold.all_applications
    -> iceberg nda_gold.dim_entity_enrichment -- async enricher
      -> iceberg nda_gold.applications_enriched
"""
import argparse
from datetime import datetime, timezone
import json
import os
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5
import requests
from .contracts import COLUMNS, PROCESSES, rules, table_names

PRODUCER = "https://github.com/nda/streaming/tree/v1"
SPEC = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
FACETS = "https://openlineage.io/spec/facets"
SQL_NS = "sqlserver://nda-sqlserver:1433"
KAFKA_NS = "kafka://nda-kafka:9092"
LAKE_NS = "iceberg://nda-lakehouse"

# Flink maps SQL Server DATETIME2 to epoch millis and FLOAT to double on the way through.
LAKE_TYPES = {"DATETIME2(3)": "bigint", "FLOAT": "double", "INT": "int"}


def lake_type(sql_type):
    return LAKE_TYPES.get(sql_type, "string")


def schema_facet(fields):
    return {"_producer": PRODUCER, "_schemaURL": f"{FACETS}/1-1-0/SchemaDatasetFacet.json",
            "fields": [{"name": n, "type": t} for n, t in fields]}


def column_lineage(inputs):
    """fields -> the upstream field(s) each column is derived from."""
    return {"_producer": PRODUCER, "_schemaURL": f"{FACETS}/1-0-1/ColumnLineageDatasetFacet.json",
            "fields": inputs}


def identity_columns(namespace, name):
    return {c: {"inputFields": [{"namespace": namespace, "name": name, "field": c}],
                "transformationDescription": "carried through unchanged",
                "transformationType": "IDENTITY"} for c in COLUMNS}


def dataset(namespace, name, fields=None, facets=None):
    entry = {"namespace": namespace, "name": name, "facets": facets or {}}
    if fields:
        entry["facets"]["schema"] = schema_facet(fields)
    return entry


def source_fields():
    return [(c, t.lower()) for c, t in COLUMNS.items()]


def lake_fields():
    return [(c, lake_type(t)) for c, t in COLUMNS.items()]


KPI_FIELDS = [("measurement_id", "string"), ("cohort_month", "string"), ("application_id", "string"),
              ("activity_id", "string"), ("process_code", "string"), ("kpi_id", "string"),
              ("reporting_quarter", "string"), ("measured_value", "double"), ("numerator", "int"),
              ("denominator", "int"), ("updated_at", "bigint")]
ENRICH_FIELDS = [("entity_id", "string"), ("legal_name", "string"), ("country", "string"),
                 ("risk_tier", "string"), ("registry_status", "string"), ("enriched_at", "timestamp"),
                 ("source_latency_ms", "bigint"), ("status", "string")]


def jobs():
    """Yield (job_name, description, inputs, outputs) for every hop."""
    for table in table_names():
        process = table.split("_", 1)[0]
        topic = f"nda.NDAStreaming.dbo.{table}"
        src = dataset(SQL_NS, f"NDAStreaming.dbo.{table}", source_fields())
        kafka = dataset(KAFKA_NS, topic, source_fields())
        yield (f"debezium.cdc.{table}", "Debezium SQL Server CDC; transaction log to Kafka",
               [src], [dataset(KAFKA_NS, topic, source_fields(),
                               {"columnLineage": column_lineage(identity_columns(SQL_NS, f"NDAStreaming.dbo.{table}"))})])
        yield (f"flink.bronze.{table}", "Raw Kafka envelope retained with partition/offset/timestamp",
               [kafka], [dataset(LAKE_NS, f"nda_bronze.{table}",
                                 [("payload", "string"), ("kafka_partition", "int"),
                                  ("kafka_offset", "bigint"), ("kafka_timestamp", "timestamp"),
                                  ("ingest_day", "string")])])
        yield (f"flink.silver.{table}", "Validated changelog upserted by (record_id, cohort_month)",
               [kafka], [dataset(LAKE_NS, f"nda_silver.{table}", lake_fields(),
                                 {"columnLineage": column_lineage(identity_columns(KAFKA_NS, topic))}),
                         dataset(LAKE_NS, f"nda_silver.quarantine_{table}", lake_fields())])
        yield (f"flink.gold.{table}", "Curated fact; same grain as silver, served to reporting",
               [dataset(LAKE_NS, f"nda_silver.{table}", lake_fields())],
               [dataset(LAKE_NS, f"nda_gold.fact_{table}", lake_fields(),
                        {"columnLineage": column_lineage(identity_columns(LAKE_NS, f"nda_silver.{table}"))})])
    for process in PROCESSES:
        source = f"nda_silver.{process.lower()}_activities"
        keys = sorted({r.key for r in rules() if r.process == process})
        target = f"nda_gold.fact_{process.lower()}_kpi_measurements"
        derived = {
            "measured_value": {"inputFields": [{"namespace": LAKE_NS, "name": source, "field": f}
                                               for f in ("completed_at", "due_at", "received_at", "outcome")],
                               "transformationDescription": f"per-indicator predicate for {len(keys)} {process} KPIs",
                               "transformationType": "AGGREGATION"},
            "reporting_quarter": {"inputFields": [{"namespace": LAKE_NS, "name": source, "field": "completed_at"}],
                                  "transformationDescription": "calendar quarter of completion",
                                  "transformationType": "IDENTITY"},
            "numerator": {"inputFields": [{"namespace": LAKE_NS, "name": source, "field": f}
                                          for f in ("completed_at", "due_at", "outcome")],
                          "transformationDescription": "1 when the success predicate holds",
                          "transformationType": "AGGREGATION"},
        }
        yield (f"flink.kpi.{process.lower()}", f"{len(keys)} {process} indicator predicates over completed activities",
               [dataset(LAKE_NS, source, lake_fields())],
               [dataset(LAKE_NS, target, KPI_FIELDS, {"columnLineage": column_lineage(derived)})])
    yield ("enrichment.entity_registry", "Asynchronous out-of-band registry lookup; never in the reporting path",
           [dataset(LAKE_NS, "nda_gold.fact_ma_applications", lake_fields())],
           [dataset(LAKE_NS, "nda_gold.dim_entity_enrichment", ENRICH_FIELDS)])
    yield ("trino.applications_enriched", "LEFT JOIN of facts to async enrichment; pending reads as NULL",
           [dataset(LAKE_NS, "nda_gold.fact_ma_applications", lake_fields()),
            dataset(LAKE_NS, "nda_gold.dim_entity_enrichment", ENRICH_FIELDS)],
           [dataset(LAKE_NS, "nda_gold.applications_enriched", lake_fields() + ENRICH_FIELDS[1:])])
    yield ("trino.kpi_quarterly", "Serving view; aggregates measurements against dim_kpi targets",
           [dataset(LAKE_NS, f"nda_gold.fact_{p.lower()}_kpi_measurements", KPI_FIELDS) for p in PROCESSES],
           [dataset(LAKE_NS, "nda_gold.kpi_quarterly",
                    [("process_code", "string"), ("kpi_id", "string"), ("reporting_quarter", "string"),
                     ("baseline", "double"), ("target", "double"), ("numerator", "bigint"),
                     ("denominator", "bigint"), ("value", "double")])])


def events(namespace="nda"):
    """A START/COMPLETE pair per job, with a stable run id so re-emitting is idempotent."""
    now = datetime.now(timezone.utc).isoformat()
    for name, description, inputs, outputs in jobs():
        run_id = str(uuid5(NAMESPACE_URL, f"nda:lineage:{name}"))
        base = {"producer": PRODUCER, "schemaURL": SPEC,
                "job": {"namespace": namespace, "name": name,
                        "facets": {"documentation": {"_producer": PRODUCER,
                                                     "_schemaURL": f"{FACETS}/1-0-1/DocumentationJobFacet.json",
                                                     "description": description}}},
                "run": {"runId": run_id}, "inputs": inputs, "outputs": outputs}
        yield dict(base, eventType="START", eventTime=now)
        yield dict(base, eventType="COMPLETE", eventTime=now)


def emit(url=None, namespace="nda"):
    url = (url or os.environ.get("OPENLINEAGE_URL", "http://127.0.0.1:5000")).rstrip("/")
    session = requests.Session()
    sent = 0
    for event in events(namespace):
        response = session.post(f"{url}/api/v1/lineage", json=event, timeout=30)
        if response.status_code >= 400:
            raise RuntimeError(f"{event['job']['name']} rejected: {response.status_code} {response.text[:200]}")
        sent += 1
    print(f"Emitted {sent} OpenLineage events for {sent // 2} jobs into namespace '{namespace}'")
    return sent


def graph(url=None, namespace="nda"):
    """Walk the emitted graph the way a catalog user would: upstream and downstream."""
    url = (url or os.environ.get("OPENLINEAGE_URL", "http://127.0.0.1:5000")).rstrip("/")
    produced, consumed = {}, {}
    for name, _, inputs, outputs in jobs():
        for i in inputs:
            consumed.setdefault(f"{i['namespace']}|{i['name']}", []).append(name)
        for o in outputs:
            produced.setdefault(f"{o['namespace']}|{o['name']}", []).append(name)
    return produced, consumed


def trace(target, depth=0, seen=None, direction="up"):
    """Recursive upstream/downstream traversal from any dataset."""
    produced, consumed = graph()
    seen = seen if seen is not None else set()
    edges = []

    def walk(node, level, way):
        if node in seen or level > 8:
            return
        seen.add(node)
        jobs_here = produced.get(node, []) if way == "up" else consumed.get(node, [])
        for job in jobs_here:
            for name, _, inputs, outputs in jobs():
                if name != job:
                    continue
                nxt = inputs if way == "up" else outputs
                for d in nxt:
                    key = f"{d['namespace']}|{d['name']}"
                    edges.append((level, job, node, key, way))
                    walk(key, level + 1, way)
    walk(target, 0, direction)
    return edges


# --- OpenMetadata -----------------------------------------------------------
# The same job graph, expressed as entity-to-entity edges. OpenMetadata models
# lineage between entities rather than through a job node, so each job becomes
# the cartesian product of its inputs and outputs, carrying the job as the
# edge description. Names must match what ingestion created exactly.
OM_SERVICE = {SQL_NS: "nda_sqlserver", KAFKA_NS: "nda_kafka", LAKE_NS: "nda_trino"}


def om_entity(namespace, name):
    """(kind, fullyQualifiedName) for an OpenMetadata lookup."""
    if namespace == SQL_NS:
        return "tables", f"nda_sqlserver.{name}"
    if namespace == KAFKA_NS:
        return "topics", f'nda_kafka."{name}"'
    return "tables", f"nda_trino.iceberg.{name}"


def publish_openmetadata(url=None, token=None):
    base = (url or os.environ["OPENMETADATA_URL"]).rstrip("/")
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + (token or os.environ["OPENMETADATA_TOKEN"])
    cache, missing = {}, set()

    def resolve(namespace, name):
        kind, fqn = om_entity(namespace, name)
        if (kind, fqn) not in cache:
            response = session.get(f"{base}/v1/{kind}/name/{quote(fqn, safe='')}", timeout=30)
            if response.status_code == 404:
                missing.add(fqn)
                cache[(kind, fqn)] = None
            else:
                response.raise_for_status()
                cache[(kind, fqn)] = {"id": response.json()["id"],
                                      "type": "table" if kind == "tables" else "topic"}
        return cache[(kind, fqn)]

    planned = []
    for name, description, inputs, outputs in jobs():
        for source in inputs:
            upstream = resolve(source["namespace"], source["name"])
            for sink in outputs:
                downstream = resolve(sink["namespace"], sink["name"])
                if upstream and downstream:
                    planned.append({"edge": {"fromEntity": upstream, "toEntity": downstream,
                                             "lineageDetails": {"description": f"{name}: {description}"}}})
    for payload in planned:
        response = session.put(f"{base}/v1/lineage", json=payload, timeout=30)
        response.raise_for_status()
    print(f"Published {len(planned)} OpenMetadata lineage edges"
          + (f"; {len(missing)} entities not yet ingested: {sorted(missing)[:3]}" if missing else ""))
    return len(planned), sorted(missing)


def main():
    parser = argparse.ArgumentParser(description="Emit or inspect NDA lineage")
    parser.add_argument("--emit", action="store_true", help="Post OpenLineage events to Marquez")
    parser.add_argument("--openmetadata", action="store_true", help="Publish the same graph into OpenMetadata")
    parser.add_argument("--url", default=None)
    parser.add_argument("--namespace", default="nda")
    parser.add_argument("--upstream", default=None, help="Dataset name to trace upstream from")
    parser.add_argument("--downstream", default=None, help="Dataset name to trace downstream from")
    args = parser.parse_args()
    if args.emit:
        emit(args.url, args.namespace)
    elif args.openmetadata:
        publish_openmetadata()
    elif args.upstream or args.downstream:
        way = "up" if args.upstream else "down"
        target = f"{LAKE_NS}|{args.upstream or args.downstream}"
        for level, job, node, nxt, _ in trace(target, direction=way):
            arrow = "<-" if way == "up" else "->"
            print(f"{'  ' * level}{node.split('|')[-1]} {arrow} [{job}] {arrow} {nxt.split('|')[-1]}")
    else:
        for name, description, inputs, outputs in jobs():
            print(f"{name}\n    in : {', '.join(i['name'] for i in inputs)}\n    out: {', '.join(o['name'] for o in outputs)}")


if __name__ == "__main__":
    main()
