# Making the data findable by people who don't know the schema

How business users search this platform in OpenMetadata, and why the
descriptions are written the way they are.

Run it with:

```bash
export OPENMETADATA_URL=http://127.0.0.1:8585/api
export OPENMETADATA_TOKEN=<jwt>
python -m streaming.catalog_docs all          # descriptions + glossary
python -m streaming.catalog_docs search --query "where are the delays"
```

---

## The problem

OpenMetadata indexes table descriptions, column descriptions, tags and glossary
terms into Elasticsearch. So search quality is a **writing problem, not a
configuration problem**. A table described as *"CDC projection of
dbo.ma_applications"* is unfindable by someone who types *"how long do licence
applications take"* — every word of their question misses.

Worse, the physical names actively mislead. Nobody outside the data team knows
that `fact_ma_applications` is where licence turnaround lives, or that
`nda_bronze` is the one layer they should **not** use.

## The one fact that shaped everything

**OpenMetadata's search ANDs the terms in a query.** This is easy to discover
and easy to miss:

| Query | Result before |
|---|---|
| `inspections` | ✅ finds activities |
| `licence` | ✅ finds applications |
| `licence approvals` | ❌ **zero results** |

"licence" appeared in the applications description and "approvals" in the
activities description. No single table had both, so the AND produced nothing.

Everything below follows from that: **every content word of a plausible question
must appear in the same description**, or the table scores zero.

## The approach

### 1. Say what the row is, in the reader's words

> One row per application received by the Authority, with the dates it moved
> through, the route it took and where it stands now.

Not "application grain fact table".

### 2. Include the questions verbatim

Users search in questions, so the questions are in the text:

> **Answers questions like:**
> - How long do applications take from submission to decision?
> - What is our current backlog?
> - Which applications are overdue?

Including the **interrogative**. Three queries failed until "Which…" and "Who…"
phrasings were added, because AND semantics needed those literal words:

| Query | Before | After |
|---|---|---|
| `which inspections were on time` | ❌ | ✅ |
| `who was compliant` | ❌ | ✅ |
| `overdue work` | ❌ | ✅ |

### 3. Carry the synonyms people type

Each description ends with the vocabulary the schema doesn't use:

> *Also known as: application, submission, dossier, file, licence, license,
> permit, registration, approval, granted, rejected, backlog, queue, workload,
> turnaround time, TAT, lead time, how long, overdue…*

### 4. Steer people to the right layer

Two mechanisms, because description text alone put raw `nda_bronze` tables above
curated `nda_gold` ones for almost every plain-language query:

- **Bronze descriptions are deliberately thin** — no question text, no synonyms.
  They say what they are ("technical audit trail") and point elsewhere. They stop
  competing.
- **Tier tags** mark importance: gold `Tier.Tier1`, silver `Tier.Tier3`, bronze
  `Tier.Tier5`. OpenMetadata uses tier in ranking and shows it in the UI.

After both, **every** successful query returns a gold table first.

### 5. A glossary for the vocabulary itself

Twelve terms — Turnaround Time, Backlog, CAPA, Reliance, Wait Time, Bottleneck
and so on — each with synonyms (`TAT`, `WIP`, `lead time`). Glossary terms are
indexed separately, so an abbreviation finds the concept even where no table
description spells it out.

### 6. Columns in plain language

All 17 CDC columns, described for a reader who has never seen the schema:

> **wait_days** — Days spent sitting in a queue waiting for someone. Usually the
> larger and more reducible of the two.

## Measured result

Twelve queries written as a non-technical user would type them. Every one
returns a result, and every one lands on the correct **gold** table:

| Query | Top result |
|---|---|
| how long do applications take | `fact_ct_applications` |
| backlog | `fact_ct_applications` |
| where are the delays | `fact_ct_steps` |
| which inspections were on time | `fact_ct_activities` |
| licence approvals | `fact_ct_activities` |
| waiting time queue | `fact_ct_steps` |
| turnaround time TAT | `fact_ct_applications` |
| are we meeting our targets | `fact_ct_kpi_measurements` |
| clinical trial safety reports | `fact_ct_activities` |
| overdue work | `fact_ct_applications` |
| who was compliant | `fact_ct_activities` |
| which stage is slowest | `fact_ct_steps` |

**12/12**, up from 8/11 before the fixes, with the layer ranking corrected.

Coverage applied: **39 tables, 471 columns, 12 glossary terms.**

## Keeping it honest

- The vocabulary lives in `streaming/contracts.py` alongside the pipeline's other
  contracts, so the catalog, the serving API and the dashboard's download page
  all name things identically. A rename happens once.
- Tests assert the questions, synonyms and interrogatives are present, and that
  every CDC column has a plain-language description — so a new column cannot
  reach the catalog undocumented.
- `catalog_docs.py` reports per-table failures instead of aborting, because one
  unhappy entity should not stop the other 38 being documented.

## Known limits

- **Ingestion and description are run by hand.** Re-run `catalog_docs all` after
  a schema change, or the catalog drifts. This belongs on the Airflow schedule
  alongside the other periodic jobs.
- **Ranking ties across processes.** `fact_ct_*` tends to win over `fact_ma_*`
  on process-neutral queries simply through scoring ties; a user still has to
  pick their process. Adding process-specific weighting or usage signals would
  break the tie by what people actually open.
- **Search is only as good as the phrasing anticipated.** The twelve queries are
  a starting set, not proof of coverage. Worth re-testing against real queries
  from the OpenMetadata query log once people use it.
