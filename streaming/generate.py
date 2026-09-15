"""Generate reviewable SQL/connector configuration from a single contract."""
import json
import os
from pathlib import Path
from .contracts import COLUMNS, rules, table_names

OUT = Path(__file__).parent / "generated"


def source_sql():
    sql = ["IF DB_ID('NDAStreaming') IS NULL CREATE DATABASE NDAStreaming;\nGO",
           "ALTER DATABASE NDAStreaming SET ALLOW_SNAPSHOT_ISOLATION ON;\nGO",
           "USE NDAStreaming;\nGO", "IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name=DB_NAME() AND is_cdc_enabled=1) EXEC sys.sp_cdc_enable_db;\nGO"]
    for table in table_names():
        process, family = table.split("_", 1)
        nullable = {"completed_at", "outcome"}
        columns = ",\n".join(f"    {k} {v} {'NULL' if k in nullable else 'NOT NULL'}" for k, v in COLUMNS.items())
        fk = "" if family == "applications" else f", CONSTRAINT fk_{table} FOREIGN KEY(application_id) REFERENCES dbo.{process}_applications(record_id)"
        sql.append(f"""IF OBJECT_ID('dbo.{table}') IS NULL
BEGIN
CREATE TABLE dbo.{table} (
{columns},
CONSTRAINT pk_{table} PRIMARY KEY(record_id),
CONSTRAINT ck_{table}_process CHECK(process_code='{process.upper()}'),
CONSTRAINT ck_{table}_dates CHECK(due_at>=received_at AND (completed_at IS NULL OR completed_at>=received_at)),
CONSTRAINT ck_{table}_measures CHECK(touch_days>=0 AND wait_days>=0 AND revision>0),
CONSTRAINT ck_{table}_state CHECK(status IN ('RECEIVED','IN_REVIEW','COMPLETED','CANCELLED'))
{fk}
);
CREATE INDEX ix_{table}_application ON dbo.{table}(application_id, received_at);
CREATE INDEX ix_{table}_status ON dbo.{table}(status, due_at) INCLUDE(completed_at);
END;
IF NOT EXISTS (SELECT 1 FROM cdc.change_tables WHERE source_object_id=OBJECT_ID('dbo.{table}'))
EXEC sys.sp_cdc_enable_table @source_schema=N'dbo', @source_name=N'{table}', @role_name=N'nda_cdc_reader', @supports_net_changes=0;
GO""")
        sql.append(f"""CREATE OR ALTER TRIGGER dbo.tr_{table}_immutable_cohort ON dbo.{table} AFTER UPDATE AS
BEGIN
SET NOCOUNT ON;
IF UPDATE(record_id) OR EXISTS (
 SELECT 1 FROM inserted i JOIN deleted d ON i.record_id=d.record_id
 WHERE i.cohort_month<>d.cohort_month OR i.application_id<>d.application_id
)
THROW 51001, 'Application identity and original cohort are immutable; correct other business dates without moving the equality partition.', 1;
END;
GO""")
    # Intake registry: the arbiter of application_id uniqueness ACROSS processes, which
    # nine separate primary keys cannot enforce. Deliberately not CDC-captured; it is an
    # intake control table, not a reportable fact.
    sql.append("""IF OBJECT_ID('dbo.application_registry') IS NULL
CREATE TABLE dbo.application_registry (
    application_id VARCHAR(64) NOT NULL,
    process_code VARCHAR(3) NOT NULL,
    source VARCHAR(16) NOT NULL,
    submitted_at DATETIME2(3) NOT NULL,
    CONSTRAINT pk_application_registry PRIMARY KEY(application_id),
    CONSTRAINT ck_application_registry_process CHECK(process_code IN ('MA','CT','GMP')),
    CONSTRAINT ck_application_registry_source CHECK(source IN ('simulation','submission'))
);
GO""")
    sql.append("EXEC sys.sp_cdc_change_job @job_type=N'cleanup', @retention=10080;\nGO")
    return "\n\n".join(sql)


def connector_config():
    return {"name": "nda-sqlserver", "config": {
        "connector.class": "io.debezium.connector.sqlserver.SqlServerConnector",
        "tasks.max": "1", "database.hostname": "nda-sqlserver", "database.port": "1433",
        "database.user": "nda_debezium", "database.password": os.environ.get("DEBEZIUM_PASSWORD", "SET_AT_DEPLOYMENT"),
        "database.names": "NDAStreaming", "topic.prefix": "nda",
        "table.include.list": ",".join(f"dbo.{t}" for t in table_names()),
        "database.encrypt": "true", "database.trustServerCertificate": "true",
        "snapshot.mode": "initial", "snapshot.isolation.mode": "snapshot",
        "schema.history.internal.kafka.bootstrap.servers": "nda-kafka:9092",
        "schema.history.internal.kafka.topic": "nda.schema-history",
        "include.schema.changes": "false", "provide.transaction.metadata": "true",
        "heartbeat.interval.ms": "10000", "tombstones.on.delete": "false",
        "decimal.handling.mode": "double", "time.precision.mode": "adaptive",
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        "key.converter.schemas.enable": "false", "value.converter.schemas.enable": "false",
        "topic.creation.default.partitions": "3", "topic.creation.default.replication.factor": "1",
        "topic.creation.default.cleanup.policy": "delete", "topic.creation.default.retention.ms": "604800000",
    }}


def flink_sql():
    statements = [
        "SET 'execution.checkpointing.interval' = '60 s'",
        "SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE'",
        "SET 'table.exec.source.cdc-events-duplicate' = 'true'",
        "SET 'table.local-time-zone' = 'UTC'",
        "SET 'pipeline.name' = 'nda-cdc-medallion-v1'",
        """CREATE CATALOG lake WITH (
 'type'='iceberg','catalog-type'='rest','uri'='http://iceberg-rest:8181',
 'warehouse'='s3://warehouse/','io-impl'='org.apache.iceberg.aws.s3.S3FileIO',
 's3.endpoint'='http://minio:9000','s3.path-style-access'='true',
 's3.multipart.part-size-bytes'='5242880')""",
    ]
    for layer in ("nda_bronze", "nda_silver", "nda_gold"):
        statements.append(f"CREATE DATABASE IF NOT EXISTS lake.{layer}")
    columns = ",\n".join(f"{k} {'BIGINT' if v.startswith('DATETIME') else 'DOUBLE' if v=='FLOAT' else 'INT' if v=='INT' else 'STRING'}" for k, v in COLUMNS.items())
    names = ", ".join(COLUMNS)
    inserts = []
    for table in table_names():
        topic = f"nda.NDAStreaming.dbo.{table}"
        statements.append(f"""CREATE TEMPORARY TABLE raw_{table} (
 payload STRING, kafka_partition INT METADATA FROM 'partition' VIRTUAL,
 kafka_offset BIGINT METADATA FROM 'offset' VIRTUAL,
 kafka_timestamp TIMESTAMP_LTZ(3) METADATA FROM 'timestamp' VIRTUAL
) WITH ('connector'='kafka','topic'='{topic}','properties.bootstrap.servers'='nda-kafka:9092',
 'properties.group.id'='nda-bronze-{table}-v1','scan.startup.mode'='earliest-offset','format'='raw')""")
        statements.append(f"""CREATE TABLE IF NOT EXISTS lake.nda_bronze.{table} (
 payload STRING, kafka_partition INT, kafka_offset BIGINT, kafka_timestamp TIMESTAMP_LTZ(3), ingest_day STRING
) PARTITIONED BY (ingest_day) WITH ('format-version'='2','write.format.default'='parquet','write.parquet.compression-codec'='zstd',
 'write.target-file-size-bytes'='33554432','write.parquet.row-group-size-bytes'='8388608')""")
        inserts.append(f"INSERT INTO lake.nda_bronze.{table} SELECT payload,kafka_partition,kafka_offset,kafka_timestamp,DATE_FORMAT(kafka_timestamp,'yyyy-MM-dd') FROM raw_{table}")
        statements.append(f"""CREATE TEMPORARY TABLE cdc_{table} (
 {columns}, PRIMARY KEY(record_id) NOT ENFORCED
) WITH ('connector'='kafka','topic'='{topic}','properties.bootstrap.servers'='nda-kafka:9092',
 'properties.group.id'='nda-facts-{table}-v1','scan.startup.mode'='earliest-offset',
 'format'='debezium-json','debezium-json.schema-include'='false','debezium-json.ignore-parse-errors'='false')""")
        for layer in ("nda_silver", "nda_gold"):
            # Immutable original application cohort is part of every equality key.
            target = table if layer == "nda_silver" else f"fact_{table}"
            statements.append(f"""CREATE TABLE IF NOT EXISTS lake.{layer}.{target} (
 {columns}, PRIMARY KEY(record_id,cohort_month) NOT ENFORCED
) PARTITIONED BY (cohort_month) WITH ('format-version'='2','write.upsert.enabled'='true',
 'write.format.default'='parquet','write.parquet.compression-codec'='zstd',
 'write.distribution-mode'='hash','write.target-file-size-bytes'='33554432',
 'write.parquet.row-group-size-bytes'='8388608')""")
            inserts.append(f"INSERT INTO lake.{layer}.{target} SELECT {names} FROM valid_{table}")
        # Invalid facts fail source constraints first. Explicit predicate is also documented
        # and surfaced in a separate quarantine sink, not dropped without evidence.
        valid = "record_id IS NOT NULL AND cohort_month IS NOT NULL AND received_at IS NOT NULL AND due_at >= received_at AND (completed_at IS NULL OR completed_at >= received_at) AND touch_days >= 0 AND wait_days >= 0 AND revision > 0"
        statements.append(f"CREATE TEMPORARY VIEW valid_{table} AS SELECT * FROM cdc_{table} WHERE {valid}")
        statements.append(f"CREATE TABLE IF NOT EXISTS lake.nda_silver.quarantine_{table} ({columns}, PRIMARY KEY(record_id,cohort_month) NOT ENFORCED) PARTITIONED BY(cohort_month) WITH ('format-version'='2','write.upsert.enabled'='true')")
        inserts.append(f"INSERT INTO lake.nda_silver.quarantine_{table} SELECT {names} FROM cdc_{table} WHERE NOT ({valid})")
    for process in ("MA", "CT", "GMP"):
        target = f"lake.nda_gold.fact_{process.lower()}_kpi_measurements"
        statements.append(f"""CREATE TABLE IF NOT EXISTS {target} (
 measurement_id STRING, cohort_month STRING, application_id STRING, activity_id STRING,
 process_code STRING, kpi_id STRING, reporting_quarter STRING,
 measured_value DOUBLE, numerator INT, denominator INT, updated_at BIGINT,
 PRIMARY KEY(measurement_id,cohort_month) NOT ENFORCED
) PARTITIONED BY(cohort_month) WITH ('format-version'='2','write.upsert.enabled'='true',
 'write.parquet.compression-codec'='zstd','write.target-file-size-bytes'='33554432',
 'write.parquet.row-group-size-bytes'='8388608')""")
        parts = []
        for rule in (r for r in rules() if r.process == process):
            where = f"activity_type='{rule.activity}' AND completed_at IS NOT NULL AND status='COMPLETED'"
            if rule.application_type:
                where += f" AND application_type='{rule.application_type}'"
            if rule.routes:
                where += " AND route IN (" + ",".join(f"'{r}'" for r in rule.routes) + ")"
            success = "outcome='COMPLIANT'" if rule.success == "compliant" else "completed_at<=due_at"
            value = f"CASE WHEN {success} THEN 100.0 ELSE 0.0 END" if rule.aggregate == "percentage" else "(completed_at-received_at)/86400000.0"
            parts.append(f"""SELECT CONCAT(record_id,':','{rule.key}'),cohort_month,application_id,record_id,
 process_code,'{rule.key}',CONCAT('Q',CAST(QUARTER(TO_TIMESTAMP_LTZ(completed_at,3)) AS STRING),' ',DATE_FORMAT(TO_TIMESTAMP_LTZ(completed_at,3),'yyyy')),
 CAST({value} AS DOUBLE),CASE WHEN {success} THEN 1 ELSE 0 END,1,updated_at
 FROM valid_{process.lower()}_activities WHERE {where}""")
        inserts.append(f"INSERT INTO {target}\n" + "\nUNION ALL\n".join(parts))
    # StatementSet branches share the normalized changelog. Do not tail a mutable
    # Iceberg table as an append-only stream (that would lose updates/deletes).
    return ";\n\n".join(statements) + ";\n\nEXECUTE STATEMENT SET\nBEGIN\n" + ";\n".join(inserts) + ";\nEND;\n"


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "01-source.sql").write_text(source_sql(), encoding="utf-8")
    (OUT / "02-debezium.json").write_text(json.dumps(connector_config(), indent=2), encoding="utf-8")
    (OUT / "03-medallion.sql").write_text(flink_sql(), encoding="utf-8")
    from .gold_sql import gold_sql
    (OUT / "04-gold-views.sql").write_text(gold_sql(), encoding="utf-8")
    print(f"Generated {len(table_names())} source tables and {len(rules())} KPI definitions in {OUT}")


if __name__ == "__main__":
    main()
