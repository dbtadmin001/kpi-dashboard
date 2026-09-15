# The dbt Semantic Layer: technical implementation

How metrics are defined, generated, compiled and governed in this platform.
For analytics engineers who will operate or extend it.

---

## The problem it solves

"Average waiting days" can be computed several defensible ways: mean of a
pre-aggregated column, weighted by volume, excluding unfinished work. When each
consumer writes its own SQL, they each pick one, and two decks disagree about the
same quarter. Nobody is wrong and nobody can prove it.

A semantic layer moves the definition out of the query and into one declaration.
Consumers ask for a **metric by name** and a **grain to slice it by**; MetricFlow
compiles the SQL. When the definition changes, every consumer's number changes
with it rather than quietly diverging.

## Where it sits

```
  nda_gold.all_steps                 physical Iceberg tables
          │
          ▼
  marketplace.delay_analysis         certified Trino view   ← access rules bite here
          │
          ▼
  semantic model: delay_analysis     entities, dimensions, measures
          │
          ▼
  metric: delay_analysis_avg_waiting_days
          │
          ▼
  mf query / compiled SQL ──────────► back to the certified view, as the caller
```

The last arrow is the important one. MetricFlow does not hold data and does not
open a privileged connection. It emits SQL against the same certified view, run
as whoever asked — so the access rules apply to metrics without the semantic
layer knowing anything about them. See [Governance](#governance) below.

## Generation: nothing here is hand-written

`marketplace/products.py` is the single declaration. `marketplace/build.py`
renders the semantic models from it, so a product cannot gain a measure without
also gaining a metric, and a metric cannot reference a column no view exposes.

```python
Product(
    name="delay_analysis",
    sql="""SELECT process_code AS process, activity_type AS workflow_stage,
                  DATE(cohort_month || '-01') AS arrival_date,
                  ROUND(AVG(wait_days), 1) AS avg_waiting_days, ...""",
    dimensions=[
        Dimension("process",        "process",        "Regulatory process"),
        Dimension("workflow_stage", "workflow_stage", "Stage in the workflow"),
        Dimension("arrival_date",   "arrival_date",   "Month the work arrived", "time"),
    ],
    measures=[
        Measure("avg_waiting_days", "avg_waiting_days", "Mean days queueing", "average"),
        Measure("times_performed",  "times_performed",  "How often the stage ran"),
    ],
)
```

`python -m marketplace.build generate` turns each `Product` into one semantic
model and one metric per measure:

| Declaration | Becomes |
|---|---|
| `Product.name` | semantic model name, and `model: ref(...)` |
| `Product.sql` | the dbt model (a view) the semantic model reads |
| `Dimension` | a categorical or time dimension |
| `Measure` | a measure **and** a simple metric |
| `Product.description` | description on both the model and the catalogue entry |

Five products → **5 semantic models, 19 metrics**.

## Anatomy of a generated semantic model

```yaml
semantic_models:
- name: delay_analysis
  description: Where elapsed time is lost, stage by stage...
  model: ref('delay_analysis')
  defaults:
    agg_time_dimension: arrival_date
  entities:
  - name: delay_analysis
    type: primary
    expr: process
  dimensions:
  - name: process
    type: categorical
    expr: process
  - name: arrival_date
    type: time
    expr: arrival_date
    type_params:
      time_granularity: month
  measures:
  - name: delay_analysis_avg_waiting_days
    agg: average
    expr: avg_waiting_days
```

Four details worth understanding, because three of them are workarounds:

**`entities` is a formality here.** MetricFlow requires every semantic model to
declare an entity, which normally names a join key. These products are
pre-aggregated marts that nobody joins across, so the generator emits a primary
entity bound to the first dimension. It satisfies the requirement and carries no
meaning. If you ever need to join two semantic models, this is the first thing
to fix — the entity must then be a genuine shared key, and `process` is not one.

**Measure names are prefixed with the product name.** MetricFlow requires measure
names to be unique across *all* semantic models, not within one. Two products
expose `avg_waiting_days`, so the measure is
`delay_analysis_avg_waiting_days` while `expr` still points at the plain column.
Without the prefix, parsing fails with a duplicate-name error that names only one
of the two offenders.

**Metric names use a single underscore.** MetricFlow rejects dunders (`__`) in
metric names — it reserves that separator for `model__dimension`. So metrics are
`delay_analysis_avg_waiting_days`, not `delay_analysis__avg_waiting_days`, even
though dimensions *are* addressed with the dunder form.

**`agg_time_dimension` makes `metric_time` work.** Every model declares its time
dimension as the default, which is what lets a consumer group by the generic
`metric_time` instead of knowing each model's column name.

## The time spine

MetricFlow will not build without a day-grain calendar, and its absence is
reported as a cryptic failure to resolve `metric_time` rather than "you need a
time spine".

```sql
{{ config(materialized='table') }}
SELECT date_day
FROM UNNEST(SEQUENCE(DATE '2025-01-01', DATE '2030-12-31', INTERVAL '1' DAY)) AS t(date_day)
```

```yaml
- name: metricflow_time_spine
  time_spine:
    standard_granularity_column: date_day
  columns:
  - name: date_day
    granularity: day
```

2,191 rows. **It is the only table the marketplace writes** — everything else is
a view. That is deliberate: the marketplace is a naming and permission layer, and
the time spine is the one thing that genuinely cannot be one.

## Time dimensions need real DATE columns

A time dimension cannot be a string. The gold layer stores cohorts as
`cohort_month` (`'2025-07'`), which reads fine but cannot be bucketed by
MetricFlow, so each product's SQL projects a real date beside it:

```sql
cohort_month                  AS arrival_month,   -- the label
DATE(cohort_month || '-01')   AS arrival_date     -- the time dimension
```

The same applies to `indicator_performance`, which carries `quarter_start`.

## How a query compiles

```bash
mf query --metrics delay_analysis_avg_waiting_days \
         --group-by delay_analysis__workflow_stage \
         --order -delay_analysis_avg_waiting_days
```

```
delay_analysis__workflow_stage      delay_analysis_avg_waiting_days
--------------------------------  ---------------------------------
GCP Inspection                                            12.3688
Safety & Efficacy Review                                  10.65
Technical Dossier Review                                   9.54444
Technical Review                                           9.39412
Ethics Review                                              8.43889
```

`--explain` shows what was actually sent:

```sql
SELECT
  workflow_stage AS delay_analysis__workflow_stage
  , AVG(avg_waiting_days) AS delay_analysis_avg_waiting_days
FROM "iceberg"."marketplace"."delay_analysis" delay_analysis_src_10000
GROUP BY
  workflow_stage
```

Note what it reads: `iceberg.marketplace.delay_analysis`, the certified view.
Never `nda_gold`. The semantic layer sits **on top of** the governed surface, not
beside it.

## Governance

Because the compiled SQL targets the certified view and runs as the calling user,
metrics inherit the access rules with no extra configuration. The restricted
product behaves exactly as it does in raw SQL:

```
  public.viewer   -> Access Denied: PERMISSION_DENIED
  alice.nakato    -> 3 rows
```

This is worth stating explicitly because it is a common failure mode elsewhere: a
semantic layer that connects with its own service account becomes a way around
the warehouse's permissions. This one does not have a service account.

Two consequences:

- A business user asking for `quality_metrics_pct_compliant` is denied, because
  they are denied the view underneath it.
- An analyst reading a physical-layer column sees `entity_id` masked, because the
  mask is on the table rule, not on the query.

## Operating it

The coordinator authenticates, so MetricFlow needs a token like any other client.

```bash
set -a && . ./streaming/.env && set +a
eval "$(python -m marketplace.auth env marketplace_owner)"   # TRINO_JWT/USER/CA
export PYTHONIOENCODING=utf-8                                # Windows only, see below

cd marketplace/dbt
dbt build --target secure        # rebuild views + time spine
mf list metrics                  # 19 metrics and their dimensions
mf list dimensions --metrics delay_analysis_avg_waiting_days
mf query --metrics ... --group-by ... --explain
```

`profiles.yml` has two targets: `secure` (JWT over TLS on 8443, the normal state)
and `dev` (plain HTTP on 8090, only valid before the OIDC client exists).

### Windows: PYTHONIOENCODING

Without it, `mf` fails with:

```
ERROR: cannot use a string pattern on a bytes-like object
```

This is not a semantic-layer problem and the message gives no hint that it is
not. MetricFlow's progress spinner prints a 🔍; the Windows console codepage
cannot encode it; `halo` falls back to writing bytes; `colorama` then applies a
string regex to those bytes. `PYTHONIOENCODING=utf-8` avoids the fallback.

## Extending it

**A new measure on an existing product** — add a `Measure` to `products.py`,
ensure the view's SQL projects that column, then:

```bash
python -m marketplace.build generate
cd marketplace/dbt && dbt build --target secure
```

**A new product** — add the `Product`, give it an audience in `PRODUCT_AUDIENCE`
(a product with no audience is invisible, which is the safe default), then the
same two commands. Access rules, semantic models and catalogue entries all follow
from the one declaration.

**Never edit `models/marketplace/semantic_models.yml` by hand.** It is generated;
the next `build generate` overwrites it, and the four surfaces drift apart.

## Current limits

- **Every metric is `type: simple`.** Ratios and derived metrics are supported by
  MetricFlow but not yet generated. The percentage measures - `pct_time_waiting`,
  `pct_on_time`, `pct_compliant` - are simple averages of a pre-computed
  percentage, which is *not* the same as a volume-weighted ratio: each month
  contributes equally regardless of how much work it contained.

  Measured on current data, the gap is small - the worst stage differs by **0.71
  percentage points** (License Publication: 35.00 simple vs 35.71 weighted) - 
  because the monthly cohorts happen to be evenly sized. That is a property of
  the data, not of the definition, and it will widen as soon as volumes become
  uneven. Expressing these as `ratio` metrics over
  `SUM(wait_days) / SUM(elapsed_days)` would be correct at any distribution; it
  needs the numerator and denominator exposed as measures first.
- **Time granularity is hardcoded to month.** The generator sets
  `time_granularity: month` for every time dimension because every product is
  aggregated at least that coarsely. Finer grains need the granularity to come
  from the `Dimension` declaration.
- **No dbt Cloud Semantic Layer API.** Consumption is via `mf` and compiled SQL.
  A BI tool integrating over JDBC should query the certified views directly; it
  will get the same numbers but not the metric definitions.
- **Entities are placeholders**, as described above. Cross-model joins would need
  real keys.

## Files

| Path | What |
|---|---|
| `marketplace/products.py` | The declaration everything is generated from |
| `marketplace/build.py` → `semantic_models()` | The generator |
| `marketplace/dbt/models/marketplace/semantic_models.yml` | Generated: 5 models, 19 metrics |
| `marketplace/dbt/models/marketplace/*.sql` | The certified views |
| `marketplace/dbt/models/marketplace/metricflow_time_spine.sql` | Day-grain calendar |
| `marketplace/dbt/models/marketplace/schema.yml` | Model docs + time-spine registration |
| `marketplace/dbt/macros/generate_schema_name.sql` | Stops dbt writing to `marketplace_marketplace` |
| `marketplace/dbt/profiles.yml` | `secure` and `dev` targets |

### The schema-name macro

dbt's default `generate_schema_name` concatenates target schema and custom
schema, producing `marketplace_marketplace`. The access rules grant on
`marketplace` exactly, so dbt would publish the certified views into a schema
nobody is entitled to read — and the failure looks like a permissions bug rather
than a naming one. The override uses the custom schema verbatim.

## Versions

| Package | Version |
|---|---|
| dbt-core | 1.12.4 |
| dbt-trino | 1.8.3 |
| dbt-metricflow | 0.15.0 |
| metricflow | 0.213.0 |
| dbt-semantic-interfaces | 0.5.1 |
