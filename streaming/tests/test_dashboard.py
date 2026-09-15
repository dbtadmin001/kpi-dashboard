import json
from streamlit.testing.v1 import AppTest
from streaming.tests.test_streaming import final_facts
from streaming.serving import snapshot
from streaming.contracts import ROOT


def test_dashboard_accepts_streaming_contract_without_estimated_fallbacks(tmp_path):
    payload = snapshot(**final_facts(8))
    payload["_meta"] = {"source": "integration-test-gold", "synthetic": True}
    fixture = tmp_path / "gold-contract.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    at = AppTest.from_file(str(ROOT / "stream_kpi_dash_g2.py"), default_timeout=45).run()
    assert not at.exception
    # The app opens on the role-gated view when identity is provisioned, so the
    # fixture path is only reachable after choosing the unauthenticated source.
    next(r for r in at.radio if r.label == "Source").set_value("Reference data").run()
    assert not at.exception
    next(t for t in at.text_input if t.label == "Data file").set_value(str(fixture)).run()
    assert not at.exception
    for process in ("MA", "CT", "GMP"):
        next(r for r in at.radio if r.label == "Process").set_value(process).run()
        assert not at.exception
        at.button[0].click().run()
        assert not at.exception
        at.button[0].click().run()
    next(r for r in at.radio if r.label == "View").set_value("Reports").run()
    assert not at.exception
    next(r for r in at.radio if r.label == "Reports View").set_value("Bottleneck Analysis").run()
    assert not at.exception
