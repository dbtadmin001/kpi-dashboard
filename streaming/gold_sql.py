"""Trino views over continuously updated physical gold facts and measurements.

Ratio literals are written as E-notation doubles: a plain 100.0 is DECIMAL(4,1)
in Trino and would silently round every percentage to one decimal place.
"""
from .contracts import reference, rules


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def kpi_query(measurements="SELECT * FROM iceberg.nda_gold.all_kpi_measurements"):
    return """WITH measurements AS (""" + measurements + """), a AS (
 SELECT process_code,kpi_id,reporting_quarter, SUM(numerator) numerator,SUM(denominator) denominator,
 AVG(measured_value) mean_value,ARRAY_SORT(ARRAY_AGG(measured_value)) ordered_values,MAX(updated_at) updated_at
 FROM measurements GROUP BY 1,2,3
)
SELECT a.process_code,a.kpi_id,a.reporting_quarter,d.baseline,d.target,a.numerator,a.denominator,a.updated_at,
 CASE WHEN d.aggregation='percentage' THEN 100.0E0*a.numerator/NULLIF(a.denominator,0)
 WHEN d.aggregation='average' THEN a.mean_value
 ELSE (ELEMENT_AT(ordered_values,CAST(CEIL(CARDINALITY(ordered_values)/2.0E0) AS INTEGER))
      +ELEMENT_AT(ordered_values,CAST(FLOOR(CARDINALITY(ordered_values)/2.0E0)+1 AS INTEGER)))/2.0E0 END value
FROM a JOIN iceberg.nda_gold.dim_kpi d ON a.process_code=d.process_code AND a.kpi_id=d.kpi_id"""


def gold_sql():
    defs = []
    for r in rules():
        spec = reference()["quarterlyData"][r.process][r.key]
        defs.append(f"({literal(r.process)},{literal(r.key)},{literal(r.aggregate)},{spec['baseline']},{spec['target']})")
    statements = ["CREATE OR REPLACE VIEW iceberg.nda_gold.dim_kpi AS SELECT * FROM (VALUES\n" + ",\n".join(defs) + ") AS t(process_code,kpi_id,aggregation,baseline,target)"]
    for family in ("applications", "activities", "steps", "kpi_measurements"):
        statements.append(f"CREATE OR REPLACE VIEW iceberg.nda_gold.all_{family} AS " + " UNION ALL ".join(f"SELECT * FROM iceberg.nda_gold.fact_{p}_{family}" for p in ("ma", "ct", "gmp")))
    statements.append("CREATE OR REPLACE VIEW iceberg.nda_gold.kpi_quarterly AS " + kpi_query())
    # Asynchronously enriched attributes are LEFT JOINed: an entity the registry has
    # not answered for yet reads as NULL with a reason, so a slow external lookup can
    # never block a report or invent a value.
    statements.append("""CREATE OR REPLACE VIEW iceberg.nda_gold.applications_enriched AS
SELECT a.*, e.legal_name, e.country, e.risk_tier, e.registry_status,
 COALESCE(e.status,'pending') enrichment_status, e.enriched_at
FROM iceberg.nda_gold.all_applications a
LEFT JOIN iceberg.nda_gold.dim_entity_enrichment e ON e.entity_id = a.entity_id""")
    return ";\n\n".join(statements) + ";\n"
