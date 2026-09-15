{{ config(materialized='view') }}
-- Delay Analysis: Where elapsed time is lost, stage by stage. The equivalent of a downtime analysis: it separates handling time from queue time so you can see which stage is the bottleneck and how much of the delay is recoverable.
SELECT
    process_code                                        AS process,
    activity_type                                       AS workflow_stage,
    cohort_month                                        AS arrival_month,
    DATE(cohort_month || '-01')                         AS arrival_date,
    COUNT(*)                                            AS times_performed,
    ROUND(AVG(touch_days), 1)                           AS avg_working_days,
    ROUND(AVG(wait_days), 1)                            AS avg_waiting_days,
    ROUND(AVG(touch_days + wait_days), 1)               AS avg_elapsed_days,
    ROUND(100.0E0 * SUM(wait_days)
          / NULLIF(SUM(touch_days + wait_days), 0), 1)  AS pct_time_waiting,
    ROUND(100.0E0 * COUNT_IF(completed_at <= due_at)
          / NULLIF(COUNT_IF(completed_at IS NOT NULL), 0), 1) AS pct_stage_on_time
FROM iceberg.nda_gold.all_steps
GROUP BY 1, 2, 3, 4
