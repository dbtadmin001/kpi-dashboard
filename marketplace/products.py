"""The data marketplace catalogue: what business users are allowed to see.

One declaration drives four things, so they cannot drift apart:

  * the certified Trino views business users query
  * the Trino access-control rules that hide everything else
  * the dbt semantic models and metrics
  * the OpenMetadata certification and descriptions

Two design rules underpin the whole thing:

1. **Views, not copies.** A view stores a definition, not data: measured here,
   five certified views occupy 4.6 KiB of metadata against 235 MiB of underlying
   gold tables - 0.002%. Near-zero, not literally zero, and the distinction is
   worth keeping honest. The marketplace is a naming and permission layer, not a
   data layer.

2. **Views run as their owner, not their caller.** Trino views default to
   DEFINER security: the view executes with the privileges of whoever created
   it. That is what lets a business user read `marketplace.quality_metrics`
   while having no grant at all on `nda_gold.fact_gmp_activities` - and it is
   why the physical schemas can be denied outright rather than merely hidden
   from a menu.

The example names in the brief (fact_sales, Revenue) belong to a sales domain.
This lakehouse is a medicines regulator, so the same products are expressed in
its actual subject matter: throughput instead of revenue, regulatory quality
instead of manufacturing quality. The pattern is identical.
"""
from dataclasses import dataclass, field
from typing import List

MARKETPLACE_SCHEMA = "marketplace"
SANDBOX_PREFIX = "sandbox_"
PHYSICAL_SCHEMAS = ("nda_bronze", "nda_silver", "nda_gold")


@dataclass(frozen=True)
class Dimension:
    name: str
    expression: str
    description: str
    type: str = "categorical"


@dataclass(frozen=True)
class Measure:
    name: str
    expression: str
    description: str
    agg: str = "sum"


@dataclass(frozen=True)
class Product:
    """One certified business asset: a view, plus what it means."""
    name: str
    title: str
    description: str
    owner: str
    domain: str
    sql: str
    dimensions: List[Dimension] = field(default_factory=list)
    measures: List[Measure] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    synonyms: str = ""
    # The physical tables behind it. Declared rather than parsed so lineage and
    # the access rules can be generated without a SQL parser.
    sources: List[str] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        return f"iceberg.{MARKETPLACE_SCHEMA}.{self.name}"


PRODUCTS: List[Product] = [
    Product(
        name="application_throughput",
        title="Application Throughput",
        description=(
            "How much regulatory work arrived and was completed each quarter, by process. "
            "The marketplace equivalent of a production-output measure: volume in, volume out, "
            "and the gap between them that becomes backlog."
        ),
        owner="regulatory-performance",
        domain="Operations",
        sources=["iceberg.nda_gold.all_applications"],
        sql="""
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
""",
        dimensions=[
            Dimension("process", "process", "Regulatory process: MA, CT or GMP"),
            Dimension("arrival_date", "arrival_date", "Month the application arrived", "time"),
            Dimension("application_type", "application_type", "New, renewal, variation, amendment or inspection"),
            Dimension("handling_route", "handling_route", "On-site or desk-based, domestic or foreign, alone or by reliance"),
        ],
        measures=[
            Measure("applications_received", "applications_received", "Applications that arrived"),
            Measure("applications_completed", "applications_completed", "Applications finished"),
            Measure("applications_open", "applications_open", "Still in progress"),
            Measure("applications_overdue", "applications_overdue", "Open and past their due date"),
        ],
        questions=[
            "How many applications did we receive and complete?",
            "What is our current backlog?",
            "Which applications are overdue?",
        ],
        synonyms="throughput, volume, output, workload, backlog, intake, received, completed, overdue, pipeline",
    ),
    Product(
        name="turnaround_performance",
        title="Turnaround Performance",
        description=(
            "How long work takes end to end, and how much of that time is genuinely spent "
            "working versus waiting in a queue. The split is the point: queue time is usually "
            "the cheaper half to remove."
        ),
        owner="regulatory-performance",
        domain="Operations",
        sources=["iceberg.nda_gold.all_activities"],
        sql="""
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
""",
        dimensions=[
            Dimension("process", "process", "Regulatory process"),
            Dimension("activity", "activity", "Evaluation, inspection, decision, query response, CAPA or report"),
            Dimension("arrival_date", "arrival_date", "Month the work arrived", "time"),
            Dimension("handling_route", "handling_route", "How the work was handled"),
        ],
        measures=[
            Measure("avg_turnaround_days", "avg_turnaround_days", "Mean calendar days from receipt to completion", "average"),
            Measure("median_turnaround_days", "median_turnaround_days", "Median calendar days", "median"),
            Measure("avg_working_days", "avg_working_days", "Days actively worked", "average"),
            Measure("avg_waiting_days", "avg_waiting_days", "Days queueing", "average"),
            Measure("pct_on_time", "pct_on_time", "Share completed on or before the due date", "average"),
        ],
        questions=[
            "How long do applications take?",
            "What is our on-time rate?",
            "How much time is spent waiting rather than working?",
        ],
        synonyms="turnaround, TAT, lead time, cycle time, duration, how long, on time, timeliness, waiting, queue",
    ),
    Product(
        name="delay_analysis",
        title="Delay Analysis",
        description=(
            "Where elapsed time is lost, stage by stage. The equivalent of a downtime analysis: "
            "it separates handling time from queue time so you can see which stage is the "
            "bottleneck and how much of the delay is recoverable."
        ),
        owner="process-improvement",
        domain="Operations",
        sources=["iceberg.nda_gold.all_steps"],
        sql="""
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
""",
        dimensions=[
            Dimension("process", "process", "Regulatory process"),
            Dimension("workflow_stage", "workflow_stage", "Stage in the regulatory workflow"),
            Dimension("arrival_date", "arrival_date", "Month the work arrived", "time"),
        ],
        measures=[
            Measure("avg_elapsed_days", "avg_elapsed_days", "Mean days the stage took", "average"),
            Measure("avg_waiting_days", "avg_waiting_days", "Mean days queueing at this stage", "average"),
            Measure("pct_time_waiting", "pct_time_waiting", "Share of elapsed time spent waiting", "average"),
            Measure("times_performed", "times_performed", "How often the stage ran"),
        ],
        questions=[
            "Where are the delays in our process?",
            "Which stage is the slowest?",
            "How much time is spent waiting rather than working?",
        ],
        synonyms="delay, delays, bottleneck, downtime, waiting, queue, idle, slow, slowest, stuck, time lost",
    ),
    Product(
        name="quality_metrics",
        title="Quality Metrics",
        description=(
            "Compliance outcomes from inspections and assessments: how many were found compliant, "
            "how many were not, and how that is trending. Restricted to analysts and above - "
            "compliance findings identify small cohorts of named facilities."
        ),
        owner="quality-assurance",
        domain="Quality",
        sources=["iceberg.nda_gold.all_activities"],
        sql="""
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
""",
        dimensions=[
            Dimension("process", "process", "Regulatory process"),
            Dimension("activity", "activity", "Inspection, assessment or CAPA"),
            Dimension("arrival_date", "arrival_date", "Month the work arrived", "time"),
            Dimension("handling_route", "handling_route", "How the work was handled"),
        ],
        measures=[
            Measure("pct_compliant", "pct_compliant", "Share found compliant", "average"),
            Measure("compliant_count", "compliant_count", "Found compliant"),
            Measure("non_compliant_count", "non_compliant_count", "Found non-compliant"),
            Measure("assessments", "assessments", "Assessments with a recorded outcome"),
        ],
        questions=[
            "How many facilities or trials were found compliant?",
            "Who was found compliant, and who was not?",
            "How is compliance trending?",
        ],
        synonyms="quality, compliance, compliant, non-compliant, inspection, audit, finding, CAPA, GCP, GMP",
    ),
    Product(
        name="indicator_performance",
        title="Indicator Performance",
        description=(
            "Every published indicator against its agreed target, by quarter. This is the "
            "certified source for the KPI dashboard - if a number appears on a slide, it "
            "should come from here."
        ),
        owner="regulatory-performance",
        domain="Performance",
        sources=["iceberg.nda_gold.kpi_quarterly"],
        sql="""
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
        WHEN kpi_id LIKE 'avg\\_%' ESCAPE '\\'
          OR kpi_id LIKE 'median\\_%' ESCAPE '\\'
            THEN IF(value <= target, 'Met target', 'Below target')
        ELSE IF(value >= target, 'Met target', 'Below target')
    END                                                 AS target_status
FROM iceberg.nda_gold.kpi_quarterly
""",
        dimensions=[
            Dimension("process", "process", "Regulatory process"),
            Dimension("indicator", "indicator", "Indicator identifier"),
            Dimension("quarter_start", "quarter_start", "Reporting quarter", "time"),
            Dimension("target_status", "target_status", "Whether the target was met"),
        ],
        measures=[
            Measure("indicator_value", "indicator_value", "Measured value", "average"),
            Measure("indicator_target", "indicator_target", "Agreed target", "average"),
        ],
        questions=[
            "Are we meeting our targets?",
            "Which indicators missed their target?",
            "How is this indicator trending?",
        ],
        synonyms="KPI, indicator, metric, target, performance, score, on-time rate, quarterly",
    ),
]

# Which roles may see which products. Everything not listed is denied, so adding
# a product without deciding its audience makes it invisible rather than public.
PRODUCT_AUDIENCE = {
    "application_throughput": ["business_user", "analyst", "data_scientist", "data_engineer", "administrator"],
    "turnaround_performance": ["business_user", "analyst", "data_scientist", "data_engineer", "administrator"],
    "delay_analysis": ["business_user", "analyst", "data_scientist", "data_engineer", "administrator"],
    "indicator_performance": ["business_user", "analyst", "data_scientist", "data_engineer", "administrator"],
    # Compliance findings name small cohorts of facilities: analysts and above.
    "quality_metrics": ["analyst", "data_scientist", "data_engineer", "administrator"],
}

ROLES = ("business_user", "analyst", "data_scientist", "data_engineer", "administrator")


def by_name(name: str) -> Product:
    for product in PRODUCTS:
        if product.name == name:
            return product
    raise KeyError(f"No such data product: {name}")


def products_for(role: str) -> List[Product]:
    return [p for p in PRODUCTS if role in PRODUCT_AUDIENCE.get(p.name, [])]


SAFE_USER = __import__("re").compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def sandbox_schema(username: str) -> str:
    """Schema name for a user's sandbox. Dots and dashes are not safe in an
    unquoted schema name, so they become underscores - and because that mapping
    is not expressible as a regex backreference, the access rules name each
    sandbox explicitly rather than matching a pattern."""
    if not SAFE_USER.match(username.lower()):
        raise ValueError(f"Refusing unsafe sandbox name: {username!r}")
    return SANDBOX_PREFIX + username.lower().replace(".", "_").replace("-", "_")


# NOT the membership list. Keycloak is the directory - see marketplace/identity.py.
#
# This exists only to bootstrap a cluster before Keycloak is up, and to give the
# tests a fixed cohort that does not depend on a running realm. Adding someone
# here does NOT give them access to a live system; adding them to a Keycloak
# group does. If the two ever disagree, Keycloak is right.
SEED_MEMBERS = {
    "administrator": ["marketplace_owner", "admin", "nda_dashboard"],
    "data_engineer": ["dana.okello", "brian.mugisha"],
    "data_scientist": ["sam.scientist"],
    "analyst": ["alice.nakato", "peter.ssemwanga", "grace.auma",
                "david.kato", "sarah.namugga", "james.opio"],
    "business_user": ["public.viewer", "chief.director"],
}


def all_users():
    """The seed cohort. Live membership comes from identity.governed_users()."""
    return sorted({u for members in SEED_MEMBERS.values() for u in members})
