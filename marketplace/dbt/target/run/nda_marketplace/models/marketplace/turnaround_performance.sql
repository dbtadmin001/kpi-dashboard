
  create or replace view
    "iceberg"."marketplace"."turnaround_performance"
  security definer
  as
    
-- Turnaround Performance: How long work takes end to end, and how much of that time is genuinely spent working versus waiting in a queue. The split is the point: queue time is usually the cheaper half to remove.
SELECT
    process_code                                        AS process,
    activity_type                                       AS activity,
    cohort_month                                        AS arrival_month,
    DATE(cohort_month || '-01')                         AS arrival_date,
    route                                               AS handling_route,
    COUNT(*)                                            AS completed_count,
    ROUND(AVG(DATE_DIFF('day',
        FROM_UNIXTIME(received_at / 1000),
        FROM_UNIXTIME(completed_at / 1000))), 1)        AS avg_turnaround_days,
    ROUND(APPROX_PERCENTILE(DATE_DIFF('day',
        FROM_UNIXTIME(received_at / 1000),
        FROM_UNIXTIME(completed_at / 1000)), 0.5), 1)   AS median_turnaround_days,
    ROUND(AVG(touch_days), 1)                           AS avg_working_days,
    ROUND(AVG(wait_days), 1)                            AS avg_waiting_days,
    ROUND(100.0E0 * COUNT_IF(completed_at <= due_at)
          / NULLIF(COUNT(*), 0), 1)                     AS pct_on_time
FROM iceberg.nda_gold.all_activities
WHERE completed_at IS NOT NULL AND status = 'COMPLETED'
GROUP BY 1, 2, 3, 4, 5
  ;
