"""Assemble the legacy dashboard contract exclusively from gold rows.

The in-memory adapter is also an exact small-data oracle for integration tests.
The API queries bounded reporting cohorts; source history is never blended in.
"""
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median
from .contracts import PROCESSES, reference, rules


def dt(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=None)


def quarter(value):
    t = dt(value)
    return f"Q{(t.month-1)//3+1} {t.year}"


def qsort(value):
    return int(value.split()[1]), int(value[1])


def snapshot(applications, activities, steps, kpi_rows=None):
    template = reference()
    result = {section: {p: {} for p in PROCESSES} for section in ("quarterlyData", "processStepData", "kpiCounts", "bottleneckData", "processStepCounts")}
    for section in ("quarterlyVolumes", "inspectionVolumes"):
        result[section] = {p: [] for p in PROCESSES}
    for p in PROCESSES:
        for key, spec in template["quarterlyData"][p].items():
            result["quarterlyData"][p][key] = {"baseline": spec["baseline"], "target": spec["target"], "data": []}
            result["kpiCounts"][p][key] = []
    # Oracle path only. Production API supplies Trino's aggregate rows.
    if kpi_rows is None:
        kpi_rows = []
        for rule in rules():
            groups = defaultdict(list)
            for a in activities:
                if a["process_code"] != rule.process or a["activity_type"] != rule.activity or not a["completed_at"] or a["status"] != "COMPLETED":
                    continue
                if rule.application_type and a["application_type"] != rule.application_type:
                    continue
                if rule.routes and a["route"] not in rule.routes:
                    continue
                groups[quarter(a["completed_at"])].append(a)
            for q, rows in groups.items():
                n = sum(a["outcome"] == "COMPLIANT" if rule.success == "compliant" else dt(a["completed_at"]) <= dt(a["due_at"]) for a in rows)
                durations = [(dt(a["completed_at"])-dt(a["received_at"])).total_seconds()/86400 for a in rows]
                value = 100*n/len(rows) if rule.aggregate == "percentage" else mean(durations) if rule.aggregate == "average" else median(durations)
                kpi_rows.append(dict(process_code=rule.process, kpi_id=rule.key, reporting_quarter=q, value=value, numerator=n, denominator=len(rows)))
    for row in kpi_rows:
        p, key, q = row["process_code"], row["kpi_id"], row["reporting_quarter"]
        result["quarterlyData"][p][key]["data"].append({"quarter": q, "value": float(row["value"])})
        if key.startswith("pct_"):
            result["kpiCounts"][p][key].append({"quarter": q, "numerator": row["numerator"], "denominator": row["denominator"]})
        else:
            result["kpiCounts"][p][key].append({"quarter": q, "sample_n": row["denominator"]})
    volumes = defaultdict(lambda: defaultdict(int))
    inspections = defaultdict(lambda: defaultdict(int))
    def increment(row, metric, timestamp, count=1):
        if timestamp is not None:
            volumes[(row["process_code"], quarter(timestamp))][metric] += count
    for a in applications:
        increment(a, "applications_received", a["received_at"])
        increment(a, "applications_completed", a["completed_at"])
        types = {"MA": {"new": "new_applications", "renewal": "renewal_applications", "variation": "variation_applications"}, "CT": {"new": "new_ct_applications", "amendment": "amendment_applications"}}
        metric = types.get(a["process_code"], {}).get(a["application_type"])
        if metric:
            increment(a, metric, a["received_at"])
        if a["process_code"] == "MA" and "reliance" in a["route"]:
            increment(a, "reliance_used_count", a["received_at"])
    mapping = {
        "MA": {"fir_response": ("fir_sent", "fir_responses_received"), "query_response": ("query_cycles_total", None), "decision": (None, "approvals_granted")},
        "CT": {"inspection": ("gcp_inspections_requested", "gcp_inspections_conducted"), "registration": (None, "trials_registered"), "safety_report": ("safety_reports_submitted", None), "capa": ("capa_requests", None)},
        "GMP": {"inspection": ("inspections_requested_total", "inspections_conducted_total"), "capa": ("capas_requested", "capas_closed"), "report": (None, "reports_published")},
    }
    for a in activities:
        p = a["process_code"]
        incoming, completed = mapping[p].get(a["activity_type"], (None, None))
        if incoming:
            increment(a, incoming, a["received_at"])
        if completed:
            increment(a, completed, a["completed_at"])
        if a["activity_type"] == "inspection":
            route = "desk" if "desk" in a["route"] else "reliance" if "reliance" in a["route"] else "domestic" if "domestic" in a["route"] else "foreign"
            inspections[(p, quarter(a["received_at"]))][f"requested_{route}"] += 1
            if a["completed_at"]:
                measures = inspections[(p, quarter(a["completed_at"]))]
                measures[f"conducted_{route}"] += 1
                compliant = a["outcome"] == "COMPLIANT"
                measures[f"compliant_{route}"] += int(compliant)
                if p == "CT":
                    measures["sites_assessed"] += 1
                    measures["compliant_sites"] += int(compliant)
                else:
                    increment(a, "compliant_facilities" if compliant else "non_compliant_facilities", a["completed_at"])
                    increment(a, {"desk": "desk_based_inspections", "reliance": "inspections_reliance_joint", "domestic": "inspections_domestic", "foreign": "inspections_foreign"}[route], a["completed_at"])
    for (p, q), values in sorted(volumes.items(), key=lambda v: qsort(v[0][1])):
        result["quarterlyVolumes"][p].append({"quarter": q, **{k: values.get(k, 0) for k in template["quarterlyVolumes"][p][0] if k != "quarter"}})
    for (p, q), values in sorted(inspections.items(), key=lambda v: qsort(v[0][1])):
        result["inspectionVolumes"][p].append({"quarter": q, **values})
    grouped = defaultdict(list)
    for s in steps:
        if s["completed_at"]:
            grouped[(s["process_code"], s["activity_type"], quarter(s["completed_at"]))].append(s)
    for (p, step, q), rows in grouped.items():
        days = [(dt(s["completed_at"])-dt(s["received_at"])).total_seconds()/86400 for s in rows]
        target = mean((dt(s["due_at"])-dt(s["received_at"])).total_seconds()/86400 for s in rows)
        result["processStepData"][p].setdefault(step, {"data": []})["data"].append({"quarter": q, "avgDays": mean(days), "targetDays": target})
        # Capacity/staffing and historic WIP are intentionally absent until the
        # corresponding facts exist. Missing information must not become fake zeroes.
        result["bottleneckData"][p].setdefault(step, []).append({"quarter": q, "cycle_time_median": median(days), "touch_median_days": median(s["touch_days"] for s in rows), "wait_median_days": median(s["wait_days"] for s in rows)})
    for process in result["quarterlyData"].values():
        for spec in process.values():
            spec["data"].sort(key=lambda row: qsort(row["quarter"]))
    for process in result["processStepData"].values():
        for spec in process.values():
            spec["data"].sort(key=lambda row: qsort(row["quarter"]))
    return result
