
  create or replace view
    "iceberg"."marketplace"."application_throughput"
  security definer
  as
    
-- Application Throughput: How much regulatory work arrived and was completed each quarter, by process. The marketplace equivalent of a production-output measure: volume in, volume out, and the gap between them that becomes backlog.
SELECT
    process_code                                        AS process,
    cohort_month                                        AS arrival_month,
    DATE(cohort_month || '-01')                         AS arrival_date,
    application_type                                    AS application_type,
    route                                               AS handling_route,
    COUNT(*)                                            AS applications_received,
    COUNT_IF(status = 'COMPLETED')                      AS applications_completed,
    COUNT_IF(status <> 'COMPLETED')                     AS applications_open,
    -- due_at is epoch milliseconds in the lake, so compare in the same units
    -- rather than casting every row to a timestamp.
    COUNT_IF(status <> 'COMPLETED'
             AND due_at < TO_UNIXTIME(CURRENT_TIMESTAMP) * 1000) AS applications_overdue
FROM iceberg.nda_gold.all_applications
GROUP BY 1, 2, 3, 4, 5
  ;
