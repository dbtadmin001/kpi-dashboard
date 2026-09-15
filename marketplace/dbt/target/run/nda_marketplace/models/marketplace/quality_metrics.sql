
  create or replace view
    "iceberg"."marketplace"."quality_metrics"
  security definer
  as
    
-- Quality Metrics: Compliance outcomes from inspections and assessments: how many were found compliant, how many were not, and how that is trending. Restricted to analysts and above - compliance findings identify small cohorts of named facilities.
SELECT
    process_code                                        AS process,
    activity_type                                       AS activity,
    cohort_month                                        AS arrival_month,
    DATE(cohort_month || '-01')                         AS arrival_date,
    route                                               AS handling_route,
    COUNT(*)                                            AS assessments,
    COUNT_IF(outcome = 'COMPLIANT')                     AS compliant_count,
    COUNT_IF(outcome = 'NON_COMPLIANT')                 AS non_compliant_count,
    ROUND(100.0E0 * COUNT_IF(outcome = 'COMPLIANT')
          / NULLIF(COUNT_IF(outcome IN ('COMPLIANT', 'NON_COMPLIANT')), 0), 1) AS pct_compliant
FROM iceberg.nda_gold.all_activities
WHERE outcome IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
  ;
