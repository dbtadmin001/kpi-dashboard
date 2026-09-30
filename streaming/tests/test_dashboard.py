import json
from streamlit.testing.v1 import AppTest
from streaming.tests.test_streaming import final_facts
from streaming.serving import snapshot
from streaming.contracts import ROOT


def test_dashboard_starts_at_the_live_authenticated_gate():
    at = AppTest.from_file(str(ROOT / "stream_kpi_dash_g2.py"), default_timeout=45).run()
    assert not at.exception
    assert not any(r.label == "Source" for r in at.radio)
    assert any(t.label == "Analytics API" for t in at.text_input)
    assert any(t.label == "Username" for t in at.text_input)
