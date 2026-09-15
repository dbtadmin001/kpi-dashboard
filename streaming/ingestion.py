"""OpenMetadata ingestion configuration, generated rather than hand-written.

These configs carry a live JWT and database passwords, so they must never be
committed. Generating them puts the connector definitions in the project while
the secrets stay in the environment and the rendered files stay gitignored.

    export OPENMETADATA_TOKEN=<jwt>
    python -m streaming.ingestion write
    python -m streaming.ingestion run          # write, copy and ingest all three
"""
import argparse
import os
import pathlib
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "streaming" / "generated" / "ingestion"
CONTAINER = os.environ.get("OM_INGESTION_CONTAINER", "openmetadata-ingestion")


def workflow_tail():
    token = os.environ.get("OPENMETADATA_TOKEN", "")
    if not token:
        raise SystemExit(
            "OPENMETADATA_TOKEN is not set. Obtain one by logging in, then export it:"
            + chr(10) + "  export OPENMETADATA_TOKEN=<jwt>")
    return {
        "sink": {"type": "metadata-rest", "config": {}},
        "workflowConfig": {
            "loggerLevel": "WARN",
            "openMetadataServerConfig": {
                # Container-to-container address, not the host-published one.
                "hostPort": os.environ.get("OPENMETADATA_INTERNAL_URL",
                                           "http://openmetadata-server:8585/api"),
                "authProvider": "openmetadata",
                "securityConfig": {"jwtToken": token},
            },
        },
    }


def configs():
    """One ingestion workflow per source system."""
    tail = workflow_tail()
    trino_host = os.environ.get("TRINO_INTERNAL_HOST", "trino-marketplace")
    trino_port = int(os.environ.get("TRINO_INTERNAL_PORT", "8080"))
    return {
        "nda_mssql.yaml": {
            "source": {
                "type": "mssql", "serviceName": "nda_sqlserver",
                "serviceConnection": {"config": {
                    "type": "Mssql", "scheme": "mssql+pytds",
                    # The read-only CDC principal: metadata ingestion never needs more.
                    "username": "nda_debezium",
                    "password": os.environ.get("DEBEZIUM_PASSWORD", ""),
                    "hostPort": "nda-sqlserver:1433", "database": "NDAStreaming",
                }},
                "sourceConfig": {"config": {
                    "type": "DatabaseMetadata", "includeTables": True, "includeViews": False,
                    "schemaFilterPattern": {"includes": ["dbo"]},
                }},
            }, **tail},
        "nda_trino.yaml": {
            "source": {
                "type": "trino", "serviceName": "nda_trino",
                "serviceConnection": {"config": {
                    # The governed coordinator, so the catalogue reflects what the
                    # marketplace actually exposes.
                    "type": "Trino", "username": "marketplace_owner",
                    "hostPort": f"{trino_host}:{trino_port}", "catalog": "iceberg",
                }},
                "sourceConfig": {"config": {
                    "type": "DatabaseMetadata", "includeTables": True, "includeViews": True,
                    "schemaFilterPattern": {"includes": [
                        "marketplace", "nda_bronze", "nda_silver", "nda_gold"]},
                }},
            }, **tail},
        "nda_kafka.yaml": {
            "source": {
                "type": "kafka", "serviceName": "nda_kafka",
                "serviceConnection": {"config": {
                    "type": "Kafka", "bootstrapServers": "nda-kafka:9092"}},
                "sourceConfig": {"config": {
                    "type": "MessagingMetadata",
                    "topicFilterPattern": {"includes": ["nda.NDAStreaming.*"]},
                }},
            }, **tail},
    }


def write():
    OUT.mkdir(parents=True, exist_ok=True)
    rendered = configs()
    for name, document in rendered.items():
        (OUT / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    print(f"Wrote {len(rendered)} ingestion configs to {OUT}")
    return OUT


def run():
    directory = write()
    for name in configs():
        source = directory / name
        subprocess.run(["docker", "cp", str(source), f"{CONTAINER}:/tmp/{name}"], check=True)
        print(f"--- ingesting {name} ---")
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "metadata", "ingest", "-c", f"/tmp/{name}"],
            capture_output=True, text=True)
        tail = [line for line in (result.stdout + result.stderr).splitlines()
                if "ERROR" in line or "Success" in line or "Processed" in line]
        print("  " + (tail[-1][:160] if tail else f"exit {result.returncode}"))


def main():
    parser = argparse.ArgumentParser(description="OpenMetadata ingestion configs")
    parser.add_argument("action", choices=["write", "run"])
    args = parser.parse_args()
    (write if args.action == "write" else run)()


if __name__ == "__main__":
    main()
