-- Returning rows fails the release. Bounds alone complement, not replace,
-- the deterministic source-to-gold assertions in delivery.integration.
select process from {{ ref('application_throughput') }}
where applications_received < 0 or applications_completed < 0
   or applications_open < 0 or applications_overdue < 0
   or applications_overdue > applications_open
union all
select process from {{ ref('quality_metrics') }}
where pct_compliant < 0 or pct_compliant > 100
   or compliant_count < 0 or non_compliant_count < 0
union all
select process from {{ ref('turnaround_performance') }}
where pct_on_time < 0 or pct_on_time > 100
   or avg_turnaround_days < 0 or avg_working_days < 0 or avg_waiting_days < 0
