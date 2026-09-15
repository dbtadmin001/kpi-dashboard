"""Business definitions. Synthetic SLA assumptions require regulatory sign-off."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
PROCESSES = ("MA", "CT", "GMP")
FAMILIES = ("applications", "activities", "steps")
ROUTES = ("on_site_domestic", "on_site_foreign", "reliance_joint_on_site_foreign",
          "reliance_joint_desk_based_foreign")


@lru_cache(maxsize=1)
def reference():
    return json.loads((ROOT / "data/kpiData.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Rule:
    key: str
    process: str
    activity: str
    aggregate: str = "percentage"
    success: str = "on_time"
    application_type: str = ""
    routes: tuple = ()
    sla_days: int = 30


# Activity grains, rather than synthetic pre-computed KPI records.
BASE = {
    "MA": [
        ("pct_new_apps_evaluated_on_time", "evaluation", "new", 90),
        ("pct_renewal_apps_evaluated_on_time", "evaluation", "renewal", 60),
        ("pct_variation_apps_evaluated_on_time", "evaluation", "variation", 30),
        ("pct_fir_responses_on_time", "fir_response", "", 30),
        ("pct_query_responses_evaluated_on_time", "query_response", "", 30),
        ("pct_granted_within_90_days", "decision", "", 90),
        ("median_duration_continental", "decision", "", 90),
    ],
    "CT": [
        ("pct_new_apps_evaluated_on_time_ct", "evaluation", "new", 60),
        ("pct_amendment_apps_evaluated_on_time", "evaluation", "amendment", 30),
        ("pct_gcp_inspections_on_time", "inspection", "", 30),
        ("pct_safety_reports_assessed_on_time", "safety_report", "", 15),
        ("pct_gcp_compliant", "inspection", "", 30),
        ("pct_registry_submissions_on_time", "registration", "", 30),
        ("pct_capa_evaluated_on_time", "capa", "", 30),
        ("avg_turnaround_time", "decision", "", 60),
    ],
    "GMP": [
        ("pct_inspections_waived_on_time", "waiver", "", 30),
        ("pct_facilities_inspected_on_time", "inspection", "", 60),
        ("pct_facilities_compliant", "inspection", "", 60),
        ("pct_capa_decisions_on_time", "capa", "", 30),
        ("pct_applications_completed_on_time", "decision", "", 90),
        ("avg_turnaround_time_gmp", "decision", "", 90),
        ("median_turnaround_time", "decision", "", 90),
        ("pct_reports_published_on_time", "report", "", 15),
    ],
}
ROUTE_ALIASES = {
    "direct_foreign_domestic_done_by_nra": ROUTES[:2],
    "reliance_rec_joint_inspections": ROUTES[2:],
    "domestic_applicant": ROUTES[:1],
    "foreign_applicant_direct": ROUTES[1:2],
    "foreign_applicant_reliance": ROUTES[2:],
    **{r: (r,) for r in ROUTES},
}


@lru_cache(maxsize=1)
def rules():
    result = []
    for process, definitions in BASE.items():
        for key in reference()["quarterlyData"][process]:
            matches = [d for d in definitions if key == d[0] or key.startswith(d[0] + "_")]
            if not matches:
                raise ValueError(f"Unmapped indicator: {process}.{key}")
            base, activity, app_type, sla = max(matches, key=lambda d: len(d[0]))
            suffix = key[len(base):].lstrip("_")
            routes = ROUTE_ALIASES[suffix] if suffix else ()
            aggregate = "median" if key.startswith("median_") else "average" if key.startswith("avg_") else "percentage"
            success = "compliant" if "compliant" in key else "on_time"
            result.append(Rule(key, process, activity, aggregate, success, app_type, routes, sla))
    return result


def table_names():
    return [f"{p.lower()}_{f}" for p in PROCESSES for f in FAMILIES]


# Common CDC representation across the nine separately keyed transaction tables.
# SQL Server datetime2(3) is represented by Debezium epoch milliseconds.
COLUMNS = {
    "record_id": "VARCHAR(64)", "application_id": "VARCHAR(64)",
    "entity_id": "VARCHAR(64)", "process_code": "VARCHAR(3)",
    "application_type": "VARCHAR(32)", "activity_type": "VARCHAR(128)",
    "route": "VARCHAR(80)", "cohort_month": "VARCHAR(7)",
    "received_at": "DATETIME2(3)", "due_at": "DATETIME2(3)",
    "completed_at": "DATETIME2(3)", "updated_at": "DATETIME2(3)",
    "status": "VARCHAR(20)", "outcome": "VARCHAR(24)",
    "touch_days": "FLOAT", "wait_days": "FLOAT", "revision": "INT",
}


# Business names for the catalogue. Nobody outside the data team knows what a
# "fact" table or a "bronze layer" is, so the API publishes what each dataset
# *is* in regulatory terms and keeps the physical names as an internal id.
RECORD_LABELS = {
    "applications": ("Applications", "One row per application received, with its dates, route and current status"),
    "activities": ("Assessments and inspections", "One row per evaluation, inspection, query response or decision"),
    "steps": ("Workflow stages", "One row per stage a file passed through, with time taken at each"),
    "kpi_measurements": ("Indicator measurements", "One row per indicator observation behind the dashboard figures"),
}
PROCESS_LABELS = {"ma": "Marketing authorization", "ct": "Clinical trials", "gmp": "Manufacturing quality (GMP)"}
DETAIL_LABELS = {
    "nda_gold": ("Reporting data", "Cleaned and ready to use. This is what the dashboard reports.", 1),
    "nda_silver": ("Full validated records", "Every field held for each record, at its latest state.", 2),
    "nda_bronze": ("Complete change history", "Every recorded change, including superseded values. Technical format.", 3),
}
