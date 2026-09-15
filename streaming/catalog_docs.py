"""Business descriptions and a glossary, written so lay users can find data.

OpenMetadata indexes descriptions, column descriptions, tags and glossary terms
into Elasticsearch. Search quality is therefore a *writing* problem, not a
configuration one: a table described as "CDC projection of dbo.ma_applications"
is unfindable by someone who types "how long do licence applications take".

Three rules are applied here, in order of how much they help:

  1. Say what the row IS, in the words a regulator uses. "One row per marketing
     authorization application" beats "fact table, application grain".
  2. Include the QUESTIONS the table answers, verbatim. Users search in
     questions, and the answer text is what matches them.
  3. Include SYNONYMS people actually type - licence, permit, dossier, turnaround,
     backlog, queue, waiting - because the schema's own vocabulary is not theirs.

The glossary carries the vocabulary itself, so "TAT" finds turnaround time even
where no table description mentions the abbreviation.
"""
import argparse
import json
import os
from urllib.parse import quote
import requests
from .contracts import COLUMNS, PROCESS_LABELS, RECORD_LABELS, table_names

# What each record type is, and what people ask of it.
RECORD_DOCS = {
    "applications": {
        "what": "One row per application received by the Authority, with the dates it moved through, the route it took and where it stands now.",
        "questions": [
            "How many applications did we receive and complete?",
            "How long do applications take from submission to decision?",
            "How many applications are still open or overdue?",
            "What is our current backlog?",
            "Which applications are overdue?",
            "What work is still open or pending?",
        ],
        "synonyms": "application, applications, submission, dossier, file, licence, license, permit, registration, request, case, approval, approved, granted, rejected, decision, outcome, received, completed, pending, open, work, overdue, backlog, queue, workload, turnaround time, TAT, lead time, how long, duration, on time, timeliness",
    },
    "activities": {
        "what": "One row per assessment, inspection, query response, safety report or decision carried out on an application.",
        "questions": [
            "How many inspections were carried out, and how many were on time?",
            "How long does an evaluation take?",
            "How many facilities or trials were found compliant?",
            "How quickly do we respond to queries?",
            "Which inspections were on time?",
            "Who was found compliant, and who was not?",
            "Which facilities or trials were non-compliant?",
        ],
        "synonyms": "activity, activities, assessment, evaluation, review, inspection, inspections, inspected, audit, decision, approval, approved, granted, licence, license, query, queries, FIR, safety report, CAPA, compliant, compliance, non-compliant, outcome, on time, timeliness, overdue, how many, how long",
    },
    "steps": {
        "what": "One row per stage a file passed through, with how long it was actively worked on versus how long it sat in a queue.",
        "questions": [
            "Where are the delays in our process?",
            "Which stage takes the longest?",
            "How much time is spent waiting rather than working?",
            "Where is the bottleneck?",
            "Which stage is the slowest?",
            "What work is stuck waiting?",
        ],
        "synonyms": "stage, stages, step, steps, workflow, process step, bottleneck, bottlenecks, delay, delays, delayed, slow, slowest, waiting, wait, queue, queueing, idle, handling time, touch time, cycle time, throughput, where, how long, time lost",
    },
    "kpi_measurements": {
        "what": "One row per indicator observation: the individual measurements that add up to the performance figures on the dashboard.",
        "questions": [
            "Are we meeting our targets?",
            "What is the on-time rate this quarter?",
            "How is this indicator trending?",
            "What sits behind the number on the dashboard?",
            "Which indicators missed their target?",
            "Who is meeting their targets?",
        ],
        "synonyms": "KPI, KPIs, indicator, indicators, metric, metrics, measure, measurement, target, targets, performance, score, on time, on-time rate, percentage, rate, quarterly, trend, meeting our targets",
    },
}

PROCESS_DOCS = {
    "ma": "Marketing authorization: approving medicines for sale, including new applications, renewals and variations.",
    "ct": "Clinical trials: approving and overseeing trials, including amendments, GCP inspections and safety reports.",
    "gmp": "Manufacturing quality (GMP): inspecting manufacturing sites and handling waivers, CAPAs and inspection reports.",
}

LAYER_DOCS = {
    "nda_bronze": "Raw change history, exactly as captured from the source system. Technical format - use the reporting tables unless you are auditing what changed.",
    "nda_silver": "Validated records, deduplicated to the latest state of each one. Full detail, ready to analyse.",
    "nda_gold": "Reporting data. Cleaned and curated - this is what the dashboard reports and what most people should use.",
}

# Columns in plain language. The schema is shared across all nine tables, so one
# description each serves every table that has them.
COLUMN_DOCS = {
    "record_id": "Unique reference for this row.",
    "application_id": "The application this row belongs to. Use it to join activities and stages back to their application.",
    "entity_id": "Pseudonymous reference for the company or applicant. Not a name - direct identifiers are held separately and are restricted.",
    "process_code": "Which regulatory process: MA (marketing authorization), CT (clinical trials) or GMP (manufacturing quality).",
    "application_type": "The kind of application - for example new, renewal, variation, amendment or inspection.",
    "activity_type": "What was done: an evaluation, inspection, query response, safety report, CAPA or decision.",
    "route": "How the work was handled - on-site or desk-based, domestic or foreign, done alone or through reliance on another regulator.",
    "cohort_month": "The month the application arrived. Used to group work by when it came in rather than when it finished.",
    "received_at": "When this piece of work arrived.",
    "due_at": "When it was due, based on the agreed service standard.",
    "completed_at": "When it was finished. Empty means it is still open.",
    "updated_at": "When this row last changed.",
    "status": "Where it stands: RECEIVED, IN_REVIEW, COMPLETED or CANCELLED.",
    "outcome": "The result - for example approved, completed, compliant or non-compliant.",
    "touch_days": "Days actually spent working on it.",
    "wait_days": "Days spent sitting in a queue waiting for someone. Usually the larger and more reducible of the two.",
    "revision": "Version number of this row. Increases every time the record changes.",
}

# The vocabulary itself. Indexed separately, so an abbreviation finds the concept
# even when no table description spells it out.
GLOSSARY = [
    ("Marketing Authorization", "Approval that allows a medicine to be sold. Covers new applications, renewals and variations.", ["MA", "licence", "license", "registration", "product approval"]),
    ("Clinical Trial", "A study of a medicine in people, which the Authority approves and oversees.", ["CT", "trial", "study", "research"]),
    ("Good Manufacturing Practice", "The standard a manufacturing site must meet, verified by inspection.", ["GMP", "manufacturing quality", "site inspection"]),
    ("Turnaround Time", "Calendar days from receiving work to completing it. Includes time spent waiting.", ["TAT", "lead time", "processing time", "how long it takes", "cycle time"]),
    ("On-time Rate", "The share of work completed on or before its due date, as a percentage.", ["timeliness", "on time", "met target", "SLA compliance", "punctuality"]),
    ("Backlog", "Work received but not yet completed. Grows when arrivals outpace completions.", ["open cases", "queue", "work in progress", "WIP", "pending", "outstanding"]),
    ("Wait Time", "Days a file spent queueing rather than being worked on. Usually the cheapest delay to remove.", ["queue time", "idle time", "waiting", "delay", "dead time"]),
    ("Touch Time", "Days a file was actively being worked on, excluding queueing.", ["handling time", "active time", "work time"]),
    ("CAPA", "Corrective and Preventive Action - what a company must do after a problem is found.", ["corrective action", "preventive action", "remediation", "follow-up"]),
    ("Reliance", "Using another trusted regulator's assessment instead of repeating the work.", ["joint inspection", "recognition", "work sharing", "mutual recognition"]),
    ("Cohort Month", "The month work arrived, used to group it by arrival rather than completion.", ["intake month", "arrival month", "received month"]),
    ("Bottleneck", "The workflow stage where files spend the most time, and so the one limiting throughput.", ["delay", "constraint", "slowest step", "pinch point"]),
]


def settings():
    return {
        "url": os.environ.get("OPENMETADATA_URL", "http://127.0.0.1:8585/api").rstrip("/"),
        "token": os.environ.get("OPENMETADATA_TOKEN", ""),
    }


def session():
    config = settings()
    client = requests.Session()
    if config["token"]:
        client.headers["Authorization"] = "Bearer " + config["token"]
    return client, config["url"]


def table_description(layer, table):
    """Assemble one searchable description for a physical table."""
    name = table[len("fact_"):] if table.startswith("fact_") else table
    process, _, record = name.partition("_")
    docs = RECORD_DOCS.get(record)
    if not docs:
        return None
    process_label = PROCESS_LABELS.get(process, process.upper())
    record_label = RECORD_LABELS.get(record, (record, ""))[0]
    if layer == "nda_bronze":
        # An audit trail, not a place a lay user should land. No question text
        # and no synonyms, so it stops out-ranking gold on plain-language search.
        return (
            f"Raw change history for {process_label.lower()} {record_label.lower()}: every recorded "
            f"change, including superseded values, exactly as captured from the source system. "
            f"Technical audit trail - for analysis use the reporting table instead."
        )
    questions = "\n".join(f"- {question}" for question in docs["questions"])
    return (
        f"**{process_label} — {record_label}**\n\n"
        f"{docs['what']}\n\n"
        f"{PROCESS_DOCS.get(process, '')}\n\n"
        f"**Answers questions like:**\n{questions}\n\n"
        f"**Detail level:** {LAYER_DOCS.get(layer, '')}\n\n"
        f"*Also known as: {docs['synonyms']}.*"
    )


def targets():
    """Every catalog entity that should carry a business description."""
    for table in table_names():
        yield f"nda_sqlserver.NDAStreaming.dbo.{table}", "nda_silver", table
        yield f"nda_trino.iceberg.nda_silver.{table}", "nda_silver", table
        yield f"nda_trino.iceberg.nda_bronze.{table}", "nda_bronze", table
        yield f"nda_trino.iceberg.nda_gold.fact_{table}", "nda_gold", f"fact_{table}"
    for process in ("ma", "ct", "gmp"):
        table = f"fact_{process}_kpi_measurements"
        yield f"nda_trino.iceberg.nda_gold.{table}", "nda_gold", table


def describe_tables():
    """PATCH business descriptions onto tables and their columns."""
    client, base = session()
    updated = skipped = columns_done = 0
    failed = []
    for fqn, layer, table in targets():
        response = client.get(f"{base}/v1/tables/name/{quote(fqn, safe='')}?fields=columns,tags", timeout=30)
        if response.status_code == 404:
            skipped += 1
            continue
        response.raise_for_status()
        entity = response.json()
        description = table_description(layer, table)
        if not description:
            skipped += 1
            continue
        # Tier is how OpenMetadata signals importance: it steers ranking and
        # tells a lay user at a glance which table they are meant to use.
        tier = {"nda_gold": "Tier.Tier1", "nda_silver": "Tier.Tier3",
                "nda_bronze": "Tier.Tier5"}.get(layer)
        patch = [{"op": "add", "path": "/description", "value": description}]
        if tier and not any(t["tagFQN"].startswith("Tier.") for t in entity.get("tags", [])):
            patch.append({"op": "add", "path": "/tags",
                          "value": list(entity.get("tags", [])) + [
                              {"tagFQN": tier, "source": "Classification",
                               "labelType": "Manual", "state": "Confirmed"}]})
        for index, column in enumerate(entity.get("columns", [])):
            text = COLUMN_DOCS.get(column["name"])
            if text and column.get("description") != text:
                patch.append({"op": "add", "path": f"/columns/{index}/description", "value": text})
                columns_done += 1
        applied = client.patch(f"{base}/v1/tables/{entity['id']}", json=patch, timeout=30,
                               headers={"Content-Type": "application/json-patch+json"})
        if applied.status_code >= 400:
            failed.append((fqn, applied.status_code, applied.text[:90]))
            continue
        updated += 1
    print(f"Described {updated} tables and {columns_done} columns ({skipped} not in the catalog yet)")
    for fqn, code, detail in failed:
        print(f"  ! {fqn}: {code} {detail}")
    return updated, columns_done


def publish_glossary(name="NDA Regulatory Terms"):
    """Create the business vocabulary. Synonyms are what make search forgiving."""
    client, base = session()
    existing = client.get(f"{base}/v1/glossaries/name/{quote(name, safe='')}", timeout=30)
    if existing.status_code == 404:
        created = client.put(f"{base}/v1/glossaries", timeout=30, json={
            "name": name, "displayName": name,
            "description": "Plain-language definitions of the terms used across NDA regulatory data, "
                           "so people can search for what they mean rather than what a column is called.",
        })
        created.raise_for_status()
    added = 0
    for term, definition, synonyms in GLOSSARY:
        payload = {"glossary": name, "name": term, "displayName": term,
                   "description": definition, "synonyms": synonyms}
        response = client.put(f"{base}/v1/glossaryTerms", json=payload, timeout=30)
        if response.status_code >= 400:
            print(f"  ! {term}: {response.status_code} {response.text[:120]}")
            continue
        added += 1
    print(f"Published glossary '{name}' with {added} terms")
    return added


def search(query, size=5):
    """What a lay user typing this into OpenMetadata would get back."""
    client, base = session()
    response = client.get(f"{base}/v1/search/query", timeout=30,
                          params={"q": query, "index": "table_search_index", "size": size})
    response.raise_for_status()
    hits = response.json().get("hits", {}).get("hits", [])
    return [(h["_source"].get("fullyQualifiedName"), round(h.get("_score", 0), 1)) for h in hits]


def main():
    parser = argparse.ArgumentParser(description="Business documentation for the catalog")
    parser.add_argument("action", choices=["describe", "glossary", "all", "search"])
    parser.add_argument("--query", default=None)
    args = parser.parse_args()
    if args.action in ("describe", "all"):
        describe_tables()
    if args.action in ("glossary", "all"):
        publish_glossary()
    if args.action == "search":
        for fqn, score in search(args.query or "how long do applications take"):
            print(f"  {score:>6}  {fqn}")


if __name__ == "__main__":
    main()
