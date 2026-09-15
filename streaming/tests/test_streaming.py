from datetime import datetime, timedelta
import pathlib
import json
import pytest
from streaming.contracts import COLUMNS, PROCESSES, reference, rules, table_names
from streaming.generate import source_sql, flink_sql, connector_config
from streaming.profile_reference import audit
from streaming.serving import snapshot
from streaming.simulator import intake, lifecycles


def final_facts(count=8):
    state = {}
    for table, row in lifecycles(count=count):
        key = table, row["record_id"]
        if key not in state or row["revision"] > state[key]["revision"]:
            state[key] = row
    return {family: [row for (table, _), row in state.items() if table.endswith("_" + family)] for family in ("applications", "activities", "steps")}


def test_complete_indicator_mapping():
    expected = {(p, k) for p, values in reference()["quarterlyData"].items() for k in values}
    assert {(r.process, r.key) for r in rules()} == expected


def test_deterministic_events_and_parent_integrity():
    events = list(lifecycles(4))
    assert events == list(lifecycles(4))
    assert [r["updated_at"] for _, r in events] == sorted(r["updated_at"] for _, r in events)
    parents, previous = set(), {}
    for table, row in events:
        assert set(row) == set(COLUMNS)
        assert row["due_at"] >= row["received_at"]
        assert row["completed_at"] is None or row["completed_at"] >= row["received_at"]
        assert row["touch_days"] >= 0 and row["wait_days"] >= 0
        if table.endswith("_applications"):
            parents.add(row["record_id"])
        else:
            assert row["application_id"] in parents
        old = previous.get((table, row["record_id"]))
        if old:
            assert row["revision"] > old["revision"]
            assert row["cohort_month"] == old["cohort_month"]
        previous[table, row["record_id"]] = row


def test_aggregate_contract_and_valid_ratios():
    facts = final_facts()
    result = snapshot(**facts)
    assert set(reference()) <= set(result)
    for process in result["kpiCounts"].values():
        for rows in process.values():
            for row in rows:
                if "numerator" in row:
                    assert 0 <= row["numerator"] <= row["denominator"]
    for process in result["quarterlyData"].values():
        for key, spec in process.items():
            if key.startswith("pct_"):
                assert all(0 <= row["value"] <= 100 for row in spec["data"])
    assert sum(row["applications_received"] for rows in result["quarterlyVolumes"].values() for row in rows) == 24


def test_no_observation_is_not_zero():
    result = snapshot([], [], [])
    assert all(not k["data"] for p in result["quarterlyData"].values() for k in p.values())


def test_replay_and_correction_change_denominator_once():
    facts = final_facts()
    before = snapshot(**facts)
    # The receiving DB and silver equality keys retain one latest row per activity.
    unique = {r["record_id"]: r for r in facts["activities"] * 2}
    facts["activities"] = list(unique.values())
    assert snapshot(**facts) == before
    target = next(r for r in facts["activities"] if r["activity_type"] == "inspection" and r["process_code"] == "CT")
    target["outcome"] = "COMPLIANT" if target["outcome"] != "COMPLIANT" else "NON_COMPLIANT"
    after = snapshot(**facts)
    assert before["kpiCounts"]["CT"]["pct_gcp_compliant"] != after["kpiCounts"]["CT"]["pct_gcp_compliant"]


def test_reference_quality_issues_are_exposed():
    kinds = {issue["kind"] for issue in audit(reference())["issues"]}
    assert {"negative_measure", "numerator_exceeds_denominator", "percentage_disagrees_with_counts"} <= kinds


def test_cdc_and_storage_guardrails():
    sql = source_sql()
    flink = flink_sql()
    assert "ALLOW_SNAPSHOT_ISOLATION ON" in sql
    assert "@retention=10080" in sql
    assert connector_config()["config"]["snapshot.mode"] == "initial"
    assert "cdc-events-duplicate' = 'true'" in flink
    assert "PRIMARY KEY(record_id,cohort_month)" in flink
    assert "ignore-parse-errors'='false'" in flink
    for table in table_names():
        assert f"quarantine_{table}" in flink
    assert "fact_ma_kpi_measurements" in flink


def test_api_auth_and_failure_no_fixture(monkeypatch):
    from fastapi.testclient import TestClient
    from streaming import api
    monkeypatch.setenv("NDA_API_KEY", "test-only-key")
    client = TestClient(api.app)
    assert client.get("/v1/dashboard").status_code == 401
    monkeypatch.setattr(api, "fetch_gold", lambda since: (_ for _ in ()).throw(ConnectionError()))
    api._cache.clear()
    response = client.get("/v1/dashboard", headers={"X-API-Key": "test-only-key"})
    assert response.status_code == 503
    assert "quarterlyData" not in response.json()


def test_as_of_leaves_open_work_without_partial_completions():
    cutoff = datetime(2025, 8, 15)
    events = list(lifecycles(30, as_of=cutoff))
    # The cutoff truncates the same ordered stream rather than altering it.
    assert events == list(lifecycles(30))[:len(events)]
    state, parents = {}, set()
    for table, row in events:
        assert row["updated_at"] <= cutoff
        state[table, row["record_id"]] = row
        if table.endswith("_applications"):
            parents.add(row["record_id"])
        else:
            assert row["application_id"] in parents
    open_rows = [r for r in state.values() if r["status"] != "COMPLETED"]
    assert open_rows, "cutoff must retain in-flight work"
    for row in open_rows:
        assert row["completed_at"] is None
        assert row["status"] in ("RECEIVED", "IN_REVIEW")
    # Every process keeps a live backlog, not only the slowest one.
    assert {r["process_code"] for r in open_rows} == set(PROCESSES)
    facts = {family: [r for (t, _), r in state.items() if t.endswith("_" + family)]
             for family in ("applications", "activities", "steps")}
    result = snapshot(**facts)
    for process in PROCESSES:
        assert all(spec["data"] for spec in result["quarterlyData"][process].values())


def test_intake_grows_with_horizon_without_rewriting_history():
    start = datetime(2025, 4, 1)
    early, late = datetime(2025, 6, 15), datetime(2025, 8, 15)
    assert intake(0, start, early) < intake(0, start, late)
    assert intake(30, start, late) == 30, "an explicit count still caps intake"
    before = list(lifecycles(0, start=start, as_of=early))
    after = list(lifecycles(0, start=start, as_of=late))
    # Admitting later applications must only append; replay stays idempotent in SQL.
    assert after[:len(before)] == before
    assert after == list(lifecycles(0, start=start, as_of=late))
    applications = lambda events: {r["record_id"] for t, r in events if t.endswith("_applications")}
    grown = applications(after) - applications(before)
    assert grown and not applications(before) - applications(after)
    # Every newly admitted application arrives after the earlier horizon.
    arrivals = {r["record_id"]: r["received_at"] for t, r in after if t.endswith("_applications")}
    assert all(arrivals[record] > early - timedelta(days=1) for record in grown)


def test_submission_contract_rejects_bad_payloads():
    from streaming.submission import validate, InvalidSubmission, TYPES
    validate("MA", "new", "on_site_domestic", "valid-application-1")
    for bad in [("ZZ", "new", "on_site_domestic", "valid-application-1"),
                ("MA", "amendment", "on_site_domestic", "valid-application-1"),
                ("MA", "new", "not_a_route", "valid-application-1"),
                ("MA", "new", "on_site_domestic", "short"),
                ("MA", "new", "on_site_domestic", "has spaces and $")]:
        with pytest.raises(InvalidSubmission):
            validate(*bad)
    # Every process must declare the application types the simulator can emit.
    assert set(TYPES) == set(PROCESSES)


def test_governance_classifies_every_column_and_masks_identifiers():
    from streaming.governance import CLASSIFICATION, RETENTION, masking_sql, pii_columns, processing_record
    assert set(CLASSIFICATION) == set(COLUMNS), "every CDC column needs a GDPR classification"
    assert "entity_id" in pii_columns("fact")
    assert "legal_name" in pii_columns("enrichment")
    statements = " ".join(masking_sql())
    for column in pii_columns("fact"):
        assert f"SHA256(CAST({column}" in statements, f"{column} must be masked, not passed through"
    assert "legal_name" in statements and "CAST(NULL AS VARCHAR) AS legal_name" in statements
    # Storage limitation must tighten as data gets rawer.
    assert RETENTION["nda_bronze"] < RETENTION["nda_silver"] < RETENTION["nda_gold"]
    record = processing_record()
    assert record["lawful_bases"] and record["retention_days"] == RETENTION


def test_lineage_graph_is_connected_end_to_end():
    from streaming.lineage import jobs, LAKE_NS, SQL_NS
    produced, consumed = {}, {}
    for name, _, inputs, outputs in jobs():
        for d in inputs:
            consumed.setdefault((d["namespace"], d["name"]), []).append(name)
        for d in outputs:
            produced.setdefault((d["namespace"], d["name"]), []).append(name)
    # Serving views must resolve upstream to a real SQL Server table.
    assert (LAKE_NS, "nda_gold.kpi_quarterly") in produced
    for table in table_names():
        assert (SQL_NS, f"NDAStreaming.dbo.{table}") in consumed, f"{table} is not a lineage source"
        assert (LAKE_NS, f"nda_gold.fact_{table}") in produced
    # No dataset may be consumed without something producing it (a broken graph).
    orphans = [d for d in consumed if d not in produced and d[0] != SQL_NS]
    assert not orphans, f"consumed but never produced: {orphans}"


def test_async_enrichment_is_bounded_and_records_failures():
    import asyncio
    from streaming.enrichment import enrich_all, COLUMNS as ENRICH_COLUMNS
    ids = [f"entity-{i}" for i in range(40)]
    records = asyncio.run(enrich_all(ids, concurrency=8))
    assert len(records) == len(ids)
    assert {r["entity_id"] for r in records} == set(ids)
    # A failed lookup is recorded with a status, never silently dropped.
    assert all(set(r) == set(ENRICH_COLUMNS) for r in records)
    assert all(r["status"] in ("enriched", "unavailable") for r in records)
    assert any(r["status"] == "enriched" for r in records)
    # The business payload is deterministic per entity; the two timing fields are
    # measured, so a failed lookup's latency legitimately varies between runs.
    def payload(rows):
        measured = {"enriched_at", "source_latency_ms"}
        return [{k: v for k, v in r.items() if k not in measured} for r in rows]
    assert payload(records) == payload(asyncio.run(enrich_all(ids, concurrency=8)))


def _rights(**overrides):
    base = {"groups": ["t"], "processes": ["MA"], "layers": ["nda_gold"], "formats": ["csv"],
            "indicators": "*", "full_dashboard": False, "row_limit": 1000, "can_export": True}
    base.update(overrides)
    return base


def test_redaction_removes_unentitled_processes_and_indicators():
    from streaming import access
    facts = final_facts()
    payload = snapshot(**facts)
    scoped = access.redact(payload, _rights(processes=["MA"]))
    assert scoped["quarterlyData"]["MA"], "entitled process must survive"
    for denied in ("CT", "GMP"):
        assert scoped["quarterlyData"][denied] == {}, f"{denied} must be emptied"
        assert scoped["quarterlyVolumes"][denied] == []
    # The shape is preserved so the dashboard renders rather than crashing.
    assert set(scoped["quarterlyData"]) == set(PROCESSES)


def test_redaction_limits_public_view_to_listed_indicators():
    from streaming import access
    allowed = ["pct_new_apps_evaluated_on_time", "pct_granted_within_90_days"]
    payload = snapshot(**final_facts())
    scoped = access.redact(payload, _rights(processes=["MA", "CT", "GMP"], indicators=allowed,
                                            layers=[], formats=[], can_export=False, row_limit=0))
    assert set(scoped["quarterlyData"]["MA"]) <= set(allowed)
    assert scoped["quarterlyData"]["MA"], "the allow-listed indicators must still be present"
    # Operational detail is not published to a view with no export entitlement.
    assert all(not v for v in scoped["bottleneckData"].values())
    assert all(not v for v in scoped["quarterlyVolumes"].values())


def test_visible_indicators_denies_unentitled_process():
    from streaming.access import visible_indicators
    rights = _rights(processes=["MA"], indicators=["a", "b"])
    assert visible_indicators(rights, "MA", ["a", "c"]) == ["a"]
    assert visible_indicators(rights, "CT", ["a"]) == []
    assert visible_indicators(_rights(indicators="*"), "MA", ["a", "c"]) == ["a", "c"]


def test_policy_declares_every_group_referenced_by_a_user():
    """Terraform generates both sides; this catches a hand-edit that breaks one."""
    import json
    generated = pathlib.Path("infra/generated/data.json")
    if not generated.exists():
        pytest.skip("terraform apply has not run")
    entitlements = json.loads(generated.read_text(encoding="utf-8"))["entitlements"]
    for name, rights in entitlements.items():
        assert set(rights["processes"]) <= set(PROCESSES), name
        assert set(rights["layers"]) <= {"nda_bronze", "nda_silver", "nda_gold"}, name
        assert set(rights["formats"]) <= {"csv", "xlsx"}, name
        # A group that can export must be able to name at least one format.
        assert bool(rights["layers"]) == bool(rights["formats"]), f"{name}: layers and formats disagree"
        # Only the public group may be restricted to an explicit indicator list.
        if rights["indicators"] != ["*"]:
            assert rights["row_limit"] == 0, f"{name}: indicator-restricted groups must not export"


def test_analytics_direction_and_target_scoring():
    """Turnaround indicators improve downwards; percentages improve upwards."""
    import analytics_answers as answers
    time_based = {"avg_turnaround_time"}
    assert answers.lower_is_better("avg_turnaround_time", time_based)
    assert answers.lower_is_better("median_turnaround_time", set())
    assert not answers.lower_is_better("pct_granted_within_90_days", time_based)

    data = {"quarterlyData": {"MA": {
        # 95% against a 90% target: met, and 5 points better than target.
        "pct_granted_within_90_days": {"target": 90, "data": [{"quarter": "Q1 2026", "value": 95.0}]},
        # 100 days against a 60-day target: missed by 40 days.
        "median_turnaround_time": {"target": 60, "data": [{"quarter": "Q1 2026", "value": 100.0}]},
    }}}
    frame = answers.indicator_frame(data, ["MA"], "Q1 2026", {}, time_based)
    rows = {r["kpi_id"]: r for _, r in frame.iterrows()}
    assert rows["pct_granted_within_90_days"]["Met target"] is True
    assert rows["pct_granted_within_90_days"]["Shortfall"] == -5.0
    assert rows["pct_granted_within_90_days"]["Unit"] == "%"
    assert rows["median_turnaround_time"]["Met target"] is False
    assert rows["median_turnaround_time"]["Shortfall"] == 40.0
    assert rows["median_turnaround_time"]["Unit"] == "days"


def test_analytics_ignores_indicators_without_a_target_or_value():
    import analytics_answers as answers
    data = {"quarterlyData": {"MA": {
        "pct_a": {"target": None, "data": [{"quarter": "Q1 2026", "value": 50.0}]},
        "pct_b": {"target": 90, "data": [{"quarter": "Q1 2026", "value": None}]},
        "pct_c": {"target": 90, "data": [{"quarter": "Q2 2026", "value": 99.0}]},
    }}}
    frame = answers.indicator_frame(data, ["MA"], "Q1 2026", {}, set())
    # A missing value or a different quarter is excluded; a missing target is
    # listed but not scored, so it cannot silently count as a pass.
    assert set(frame["kpi_id"]) == {"pct_a"}
    assert frame.iloc[0]["Met target"] is None


def test_every_question_has_an_answer_and_a_hint():
    import analytics_answers as answers
    assert set(answers.ANSWERS) == set(answers.QUESTIONS)
    assert set(answers.HINTS) == set(answers.QUESTIONS)
    for question in answers.QUESTIONS:
        assert question.endswith("?"), "questions should read as questions"
        assert callable(answers.ANSWERS[question])


def test_catalog_descriptions_are_written_for_search():
    """Descriptions must carry the words a lay user types, not the schema's."""
    from streaming.catalog_docs import COLUMN_DOCS, RECORD_DOCS, table_description

    gold = table_description("nda_gold", "fact_ma_applications")
    assert "Marketing authorization" in gold
    # The questions users actually ask have to be present verbatim: OpenMetadata's
    # search ANDs the query terms, so a missing word means zero results.
    assert "How long do applications take" in gold
    assert "backlog" in gold.lower() and "turnaround" in gold.lower()
    # Interrogatives matter for the same reason.
    activities = table_description("nda_gold", "fact_ct_activities")
    assert "Which inspections were on time?" in activities
    assert "Who was found compliant" in activities

    # Bronze is an audit trail: no question text, so it stops out-ranking gold.
    bronze = table_description("nda_bronze", "ma_applications")
    assert "Answers questions like" not in bronze
    assert "audit trail" in bronze.lower()

    # Every CDC column is explained in plain language, with no leftover jargon.
    assert set(COLUMN_DOCS) == set(COLUMNS)
    for column, text in COLUMN_DOCS.items():
        assert text and text[0].isupper() and text.endswith("."), column
    for record, docs in RECORD_DOCS.items():
        assert docs["questions"] and docs["synonyms"], record
        assert all(q.endswith("?") for q in docs["questions"]), record


def test_glossary_terms_carry_the_abbreviations_people_type():
    from streaming.catalog_docs import GLOSSARY
    terms = {name: synonyms for name, _, synonyms in GLOSSARY}
    assert "TAT" in terms["Turnaround Time"]
    assert "MA" in terms["Marketing Authorization"]
    assert "WIP" in terms["Backlog"]
    for name, definition, synonyms in GLOSSARY:
        assert definition.endswith("."), name
        assert synonyms, f"{name} needs synonyms or search will not find it"
