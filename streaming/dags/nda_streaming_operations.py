"""Install this file into the EXISTING Airflow DAG mount after validation.

Connections: nda_trino (HTTP endpoint, login), nda_flink (HTTP endpoint),
nda_connect (HTTP endpoint). Connections must use internal service DNS.
No streaming job is scheduled or restarted by this DAG.
"""
from datetime import datetime, timedelta
import json
import time
from urllib.request import Request, urlopen
from airflow import DAG
from airflow.hooks.base import BaseHook
from airflow.operators.python import PythonOperator


def endpoint(connection_id):
    c = BaseHook.get_connection(connection_id)
    return f"{c.schema or 'http'}://{c.host}:{c.port}", c


def get_json(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def verify_stream():
    connect, _ = endpoint("nda_connect")
    status = get_json(connect + "/connectors/nda-sqlserver/status")
    if status["connector"]["state"] != "RUNNING" or not status["tasks"] or any(t["state"] != "RUNNING" for t in status["tasks"]):
        raise RuntimeError("Debezium connector/task is not running")
    flink, _ = endpoint("nda_flink")
    jobs = [j for j in get_json(flink + "/jobs/overview")["jobs"] if j["name"] == "nda-cdc-medallion-v1" and j["state"] == "RUNNING"]
    if len(jobs) != 1:
        raise RuntimeError("Expected exactly one running NDA Flink job")
    checkpoint = get_json(flink + f"/jobs/{jobs[0]['jid']}/checkpoints")
    completed = checkpoint.get("latest", {}).get("completed")
    if not completed or time.time()*1000 - completed["latest_ack_timestamp"] > 300000:
        raise RuntimeError("No successful Flink checkpoint in the last five minutes")
    return {"job_id": jobs[0]["jid"], "checkpoint_id": completed["id"], "checkpoint_at": completed["latest_ack_timestamp"]}


def trino(sql):
    base, c = endpoint("nda_trino")
    headers = {"X-Trino-User": c.login or "nda_operations", "X-Trino-Catalog": "iceberg", "X-Trino-Schema": "nda_gold"}
    with urlopen(Request(base + "/v1/statement", data=sql.encode(), headers=headers), timeout=30) as response:
        result = json.load(response)
    rows = []
    deadline = time.monotonic() + 600
    while True:
        if "error" in result:
            raise RuntimeError(result["error"]["message"])
        rows.extend(result.get("data", []))
        if "nextUri" not in result:
            return rows
        if time.monotonic() >= deadline:
            raise TimeoutError("Trino maintenance query exceeded 10 minutes")
        result = get_json(result["nextUri"])


def verify_quality():
    violations = trino("SELECT count(*) FROM kpi_quarterly WHERE numerator > denominator OR numerator < 0 OR denominator <= 0")
    if violations[0][0]:
        raise RuntimeError("Invalid KPI counts in gold")
    for p in ("ma", "ct", "gmp"):
        for family in ("applications", "activities", "steps"):
            if trino(f"SELECT count(*) FROM iceberg.nda_silver.quarantine_{p}_{family}")[0][0]:
                raise RuntimeError(f"Quarantined records in {p}_{family}")


def compact():
    # Namespace allowlist prevents maintenance of the shared ATC tables.
    for p in ("ma", "ct", "gmp"):
        for family in ("applications", "activities", "steps", "kpi_measurements"):
            trino(f"ALTER TABLE fact_{p}_{family} EXECUTE optimize(file_size_threshold => '128MB')")


with DAG("nda_streaming_operations", start_date=datetime(2026, 1, 1),
         schedule="0 */6 * * *", catchup=False, max_active_runs=1,
         default_args={"retries": 2, "retry_delay": timedelta(minutes=2)},
         tags=["nda", "streaming", "iceberg"], is_paused_upon_creation=True) as dag:
    health = PythonOperator(task_id="verify_cdc_and_checkpoint", python_callable=verify_stream)
    quality = PythonOperator(task_id="verify_gold_quality", python_callable=verify_quality)
    maintenance = PythonOperator(task_id="compact_gold_files", python_callable=compact)
    health >> quality >> maintenance
