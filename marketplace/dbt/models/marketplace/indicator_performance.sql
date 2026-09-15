{{ config(materialized='view') }}
-- Indicator Performance: Every published indicator against its agreed target, by quarter. This is the certified source for the KPI dashboard - if a number appears on a slide, it should come from here.
SELECT
    process_code                                        AS process,
    kpi_id                                              AS indicator,
    reporting_quarter                                   AS quarter,
    -- 'Q3 2026' -> 2026-07-01, so the semantic layer has a real date to bucket on.
    DATE(SUBSTR(reporting_quarter, 4, 4) || '-'
         || LPAD(CAST((CAST(SUBSTR(reporting_quarter, 2, 1) AS INTEGER) - 1) * 3 + 1 AS VARCHAR), 2, '0')
         || '-01')                                      AS quarter_start,
    value                                               AS indicator_value,
    target                                              AS indicator_target,
    baseline                                            AS indicator_baseline,
    numerator,
    denominator,
    CASE
        WHEN target IS NULL THEN 'No target'
        WHEN kpi_id LIKE 'avg\_%' ESCAPE '\'
          OR kpi_id LIKE 'median\_%' ESCAPE '\'
            THEN IF(value <= target, 'Met target', 'Below target')
        ELSE IF(value >= target, 'Met target', 'Below target')
    END                                                 AS target_status
FROM iceberg.nda_gold.kpi_quarterly
