"""Explicit, targeted provisioning after deployment prerequisites are resolved.

No Docker lifecycle operations. Each subcommand only touches NDA namespaces.
"""
import argparse
import os
import re
import requests
from .generate import source_sql, connector_config
from .contracts import table_names


def sql_literal(value):
    return "N'" + value.replace("'", "''") + "'"


def source():
    import pymssql
    for password_key in ("SQLSERVER_PASSWORD", "DEBEZIUM_PASSWORD", "ERASURE_PASSWORD"):
        password = os.environ[password_key]
        if len(password) < 16 or "replace" in password.lower():
            raise ValueError(f"Set a strong {password_key}")
    conn = pymssql.connect(server=os.environ.get("SQLSERVER_HOST", "127.0.0.1"),
                           port=int(os.environ.get("SQLSERVER_PORT", "14333")), user="sa",
                           password=os.environ["MSSQL_SA_PASSWORD"], database="master", autocommit=True)
    try:
        cursor = conn.cursor()
        # Source DDL is additive/idempotent and confined to a new named database.
        for batch in re.split(r"(?m)^GO\s*$", source_sql()):
            if batch.strip():
                cursor.execute(batch)
        for username, password_key in (("nda_simulator", "SQLSERVER_PASSWORD"), ("nda_debezium", "DEBEZIUM_PASSWORD"),
                                       ("nda_erasure", "ERASURE_PASSWORD")):
            password = os.environ[password_key]
            cursor.execute(f"USE master; IF SUSER_ID('{username}') IS NULL CREATE LOGIN {username} WITH PASSWORD={sql_literal(password)};")
            cursor.execute(f"USE NDAStreaming; IF USER_ID('{username}') IS NULL CREATE USER {username} FOR LOGIN {username};")
        for table in table_names():
            cursor.execute(f"GRANT SELECT, INSERT, UPDATE ON dbo.{table} TO nda_simulator; GRANT SELECT ON dbo.{table} TO nda_debezium;")
        # Intake registry is written by the application path only; CDC never reads it.
        cursor.execute("GRANT SELECT, INSERT ON dbo.application_registry TO nda_simulator;")
        # GDPR Art.17 erasure is the ONLY path allowed to DELETE, and it is a separate
        # principal: the simulator must never be able to destroy regulatory records.
        for table in table_names():
            cursor.execute(f"GRANT SELECT, DELETE ON dbo.{table} TO nda_erasure;")
        cursor.execute("GRANT SELECT, DELETE ON dbo.application_registry TO nda_erasure;")
        # Backfill ids seeded before the registry existed, so they cannot be re-submitted.
        for process in ("ma", "ct", "gmp"):
            cursor.execute(f"""INSERT INTO dbo.application_registry (application_id, process_code, source, submitted_at)
SELECT a.record_id, a.process_code, 'simulation', a.received_at FROM dbo.{process}_applications a
WHERE NOT EXISTS (SELECT 1 FROM dbo.application_registry r WHERE r.application_id = a.record_id);""")
        # The gating role covers the _CT tables; Debezium also reads cdc metadata
        # (captured_columns, change_tables, lsn_time_mapping) to build table schemas.
        cursor.execute("ALTER ROLE nda_cdc_reader ADD MEMBER nda_debezium; GRANT VIEW DATABASE STATE TO nda_debezium;"
                       " GRANT SELECT ON SCHEMA::cdc TO nda_debezium;")
        print("Provisioned NDAStreaming schema and separate simulator/CDC principals")
    finally:
        conn.close()


def connector():
    config = connector_config()
    if config["config"]["database.password"] == "SET_AT_DEPLOYMENT":
        raise ValueError("Set DEBEZIUM_PASSWORD")
    base = os.environ.get("CONNECT_URL", "http://127.0.0.1:8083")
    existing = requests.get(base + "/connectors/nda-sqlserver", timeout=30)
    if existing.status_code == 200:
        print("NDA connector already exists; no reconfiguration or snapshot reset performed")
        return
    if existing.status_code != 404:
        existing.raise_for_status()
    # Connect validates a flat connector config, which must carry its own name.
    checked = requests.put(base + "/connector-plugins/io.debezium.connector.sqlserver.SqlServerConnector/config/validate",
                           json=dict(config["config"], name=config["name"]), timeout=60)
    checked.raise_for_status()
    if checked.json().get("error_count"):
        # Avoid dumping config values or passwords in the validation response.
        raise ValueError("Debezium config validation failed; inspect Connect validation securely")
    created = requests.post(base + "/connectors", json=config, timeout=60)
    created.raise_for_status()
    print("Created nda-sqlserver connector")


def gold():
    from .api import connection
    from .gold_sql import gold_sql
    conn = connection()
    try:
        cursor = conn.cursor()
        for statement in gold_sql().split(";"):
            if statement.strip():
                cursor.execute(statement)
                cursor.fetchall()
        print("Applied NDA gold views")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=["source", "connector", "gold"])
    {"source": source, "connector": connector, "gold": gold}[parser.parse_args().component]()
