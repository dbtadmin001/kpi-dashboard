
-- Day grain calendar for MetricFlow. Small and static; the only table the
-- marketplace writes, everything else is a view.
SELECT date_day
FROM UNNEST(SEQUENCE(DATE '2025-01-01', DATE '2030-12-31', INTERVAL '1' DAY)) AS t(date_day)