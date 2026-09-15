"""
National Drug Authority (NDA) Regulatory KPI Dashboard
======================================================

This Streamlit application provides an interactive dashboard for monitoring and analyzing
Key Performance Indicators (KPIs) across regulatory processes: Marketing Authorization (MA),
Clinical Trials (CT), and Good Manufacturing Practice (GMP). It supports data visualization,
trend analysis, volume comparisons, bottleneck identification, and self-service analytics.

Key Features:
- Executive overview with KPI status summaries and process step performance.
- Drill-down into individual KPIs with trends, volume breakdowns, and workflow bottlenecks.
- Self-service analytics for custom correlations, trends, and comparisons.
- Responsive design with NDA-branded theming.

Data Requirements:
- JSON file with structure: quarterlyData, processStepData, kpiCounts, quarterlyVolumes,
  inspectionVolumes, bottleneckData.

Usage:
- Run with `streamlit run app.py`.
- Configure data path in sidebar.
- Navigate via tabs and sidebar filters.

Author: NDA Analytics Team
Version: 4.2 (Enhanced Analytics)
Last Updated: November 11, 2025
"""

import json
import re
from contextlib import nullcontext
import os
import time
import requests
import pathlib
import io
import random
from typing import Dict, Any, List, Tuple, Optional
import streamlit as st
import pandas as pd
import analytics_answers
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from html import escape
import numpy as np
from scipy import stats  # For correlation/regression insights


# =======================
# PAGE CONFIGURATION
# =======================
st.set_page_config(
    page_title="Regulatory KPI Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =======================
# THEME TOKENS
# =======================
# Color palette inspired by NDA branding
NDA_GREEN = "#006341"
NDA_LIGHT_GREEN = "#e0f0e5"
NDA_DARK_GREEN = "#004c30"
NDA_ACCENT = "#8dc63f"
TEXT_DARK = "#0f172a"
TEXT_LIGHT = "#64748b"
BG_COLOR = "#f8fafc"
CARD_BG = "#ffffff"
BORDER_COLOR = "#e2e8f0"

PALETTE = {
    "primary": NDA_GREEN,
    "accent": NDA_ACCENT,
    "ok": NDA_GREEN,
    "warn": "#F59E0B",
    "bad": "#C62828",
    "info": "#1976D2",
    "violet": "#7a4cff",
    "grey": "#6b7280",
}

# GMP-specific disaggregation colors
GMP_GROUP_COLORS = {
    "Domestic": "#3b82f6",
    "Foreign": "#7c3aed",
    "Reliance": "#f59e0b",
    "Desk": "#10b981",
}


# =======================
# GLOBAL CSS STYLING
# =======================
# Shared visual language for every chart and native Streamlit control.
pio.templates["nda"] = go.layout.Template(
    layout=dict(
        font=dict(family="Inter, Arial, sans-serif", size=13, color=TEXT_DARK),
        colorway=[NDA_GREEN, "#287DA8", "#D49A32", "#8469A5", "#B85C54"],
        paper_bgcolor="white", plot_bgcolor="white",
        xaxis=dict(showgrid=False, zeroline=False, linecolor="#DCE3E6", automargin=True),
        yaxis=dict(gridcolor="#EDF1F3", zeroline=False, automargin=True),
        legend=dict(orientation="h", y=-0.18, x=0, title_text=""),
        hoverlabel=dict(bgcolor="#17352E", font_color="white"),
        margin=dict(l=24, r=24, t=44, b=48),
    )
)
pio.templates.default = "plotly_white+nda"
px.defaults.template = "plotly_white+nda"

st.markdown(
    "<style>" + pathlib.Path(__file__).with_name("dashboard_theme.css").read_text(encoding="utf-8") + "</style>",
    unsafe_allow_html=True,
)


# =======================
# UI HELPER FUNCTIONS
# =======================
def section_header(title: str, icon: str = "✅") -> None:
    """
    Render a styled section header with optional icon.

    Args:
        title (str): The section title.
        icon (str): Optional icon emoji.
    """
    st.markdown(f"""<div class="section-header">{escape(title)}</div>""", unsafe_allow_html=True)


def panel_open(title: str, icon: str = "") -> None:
    """Render a section heading; native containers own widget layout."""
    section_header(title, "")


def panel_close() -> None:
    """Retained for section call sites; no cross-element HTML wrappers."""
    pass


# =======================
# KPI MAPPING AND UTILITIES
# =======================
KPI_NAME_MAP: Dict[str, Dict[str, str]] = {
    # Marketing Authorization (MA) KPIs
    "pct_new_apps_evaluated_on_time": {"short": "New Apps on Time", "long": "Percentage of New Applications Evaluated On Time"},
    "pct_renewal_apps_evaluated_on_time": {"short": "Renewals on Time", "long": "Percentage of Renewal Applications Evaluated On Time"},
    "pct_variation_apps_evaluated_on_time": {"short": "Variations on Time", "long": "Percentage of Variation Applications Evaluated On Time"},
    "pct_fir_responses_on_time": {"short": "F.I.R Responses on Time", "long": "Percentage of Further Information Responses On Time"},
    "pct_query_responses_evaluated_on_time": {"short": "Query Responses on Time", "long": "Percentage of Query Responses Evaluated On Time"},
    "pct_granted_within_90_days": {"short": "Granted ≤ 90 Days", "long": "Percentage of Applications Granted Within 90 Days"},
    "median_duration_continental": {"short": "Median Duration", "long": "Median Duration to Grant (Days, Continental)"},
    # Clinical Trials (CT) KPIs
    "pct_new_apps_evaluated_on_time_ct": {"short": "CT New Apps on Time", "long": "Clinical Trials: % of New Applications Evaluated On Time"},
    "pct_amendment_apps_evaluated_on_time": {"short": "Amendments on Time", "long": "Clinical Trials: % of Amendment Applications Evaluated On Time"},
    "pct_gcp_inspections_on_time": {"short": "GCP Inspections on Time", "long": "Clinical Trials: % of GCP Inspections Completed On Time"},
    "pct_safety_reports_assessed_on_time": {"short": "Safety Reports on Time", "long": "Clinical Trials: % of Safety Reports Assessed On Time"},
    "pct_gcp_compliant": {"short": "GCP Compliant", "long": "Clinical Trials: % of Sites Compliant with GCP"},
    "pct_registry_submissions_on_time": {"short": "Registry on Time", "long": "Clinical Trials: % of Registry Submissions On Time"},
    "pct_capa_evaluated_on_time": {"short": "CAPA on Time", "long": "Clinical Trials: % of CAPA Evaluations Completed On Time"},
    "avg_turnaround_time": {"short": "Avg TAT (Days)", "long": "Clinical Trials: Average Turnaround Time (Days)"},
    # GMP KPIs
    "pct_facilities_inspected_on_time": {"short": "Facilities Inspected On Time", "long": "GMP: % of Facilities Inspected On Time"},
    "pct_inspections_waived_on_time": {"short": "Waivers on Time", "long": "GMP: % of Inspections Waived On Time"},
    "pct_facilities_compliant": {"short": "Facilities Compliant", "long": "GMP: % of Facilities Compliant"},
    "pct_capa_decisions_on_time": {"short": "CAPA Decisions on Time", "long": "GMP: % of CAPA Decisions On Time"},
    "pct_applications_completed_on_time": {"short": "Apps Completed on Time", "long": "GMP: % of Applications Completed On Time"},
    "avg_turnaround_time_gmp": {"short": "Avg TAT (GMP)", "long": "GMP: Average Turnaround Time (Days)"},
    "median_turnaround_time": {"short": "Median TAT", "long": "GMP: Median Turnaround Time (Days)"},
    "pct_reports_published_on_time": {"short": "Reports on Time", "long": "GMP: % of Reports Published On Time"},
    # GMP Disaggregated KPIs (child metrics)
    "pct_facilities_inspected_on_time_on_site_domestic": {"short": "On-time (On-site Domestic)", "long": "GMP: % On Time (On-site Domestic)"},
    "pct_facilities_inspected_on_time_on_site_foreign": {"short": "On-time (On-site Foreign)", "long": "GMP: % On Time (On-site Foreign)"},
    "pct_facilities_inspected_on_time_reliance_joint_on_site_foreign": {"short": "On-time (Reliance/Joint On-site Foreign)", "long": "GMP: % On Time (Reliance/Joint On-site Foreign)"},
    "pct_facilities_compliant_on_site_domestic": {"short": "Compliant (On-site Domestic)", "long": "GMP: % Compliant (On-site Domestic)"},
    "pct_facilities_compliant_on_site_foreign": {"short": "Compliant (On-site Foreign)", "long": "GMP: % Compliant (On-site Foreign)"},
    "pct_facilities_compliant_reliance_joint_on_site_foreign": {"short": "Compliant (Reliance/Joint On-site Foreign)", "long": "GMP: % Compliant (Reliance/Joint On-site Foreign)"},
    "pct_facilities_compliant_reliance_joint_desk_based_foreign": {"short": "Compliant (Reliance/Joint Desk-based Foreign)", "long": "GMP: % Compliant (Reliance/Joint Desk-based Foreign)"},
    "pct_capa_decisions_on_time_direct_foreign_domestic_done_by_nra": {"short": "CAPA on Time (Direct NRA)", "long": "GMP: % CAPA On Time (Direct NRA)"},
    "pct_capa_decisions_on_time_reliance_rec_joint_inspections": {"short": "CAPA on Time (Reliance Joint)", "long": "GMP: % CAPA On Time (Reliance Joint)"},
    "pct_applications_completed_on_time_domestic_applicant": {"short": "Apps On-time (Domestic Applicant)", "long": "GMP: % Apps On Time (Domestic Applicant)"},
    "pct_applications_completed_on_time_foreign_applicant_direct": {"short": "Apps On-time (Foreign Direct)", "long": "GMP: % Apps On Time (Foreign Direct)"},
    "pct_applications_completed_on_time_foreign_applicant_reliance": {"short": "Apps On-time (Foreign Reliance)", "long": "GMP: % Apps On Time (Foreign Reliance)"},
    "avg_turnaround_time_gmp_on_site_domestic": {"short": "Avg TAT (On-site Domestic)", "long": "GMP: Avg TAT (On-site Domestic)"},
    "avg_turnaround_time_gmp_on_site_foreign": {"short": "Avg TAT (On-site Foreign)", "long": "GMP: Avg TAT (On-site Foreign)"},
    "avg_turnaround_time_gmp_reliance_joint_on_site_foreign": {"short": "Avg TAT (Reliance/Joint On-site Foreign)", "long": "GMP: Avg TAT (Reliance/Joint On-site Foreign)"},
    "median_turnaround_time_on_site_domestic": {"short": "Median TAT (On-site Domestic)", "long": "GMP: Median TAT (On-site Domestic)"},
    "median_turnaround_time_on_site_foreign": {"short": "Median TAT (On-site Foreign)", "long": "GMP: Median TAT (On-site Foreign)"},
    "pct_reports_published_on_time_on_site_domestic": {"short": "Reports on Time (On-site Domestic)", "long": "GMP: % Reports On Time (On-site Domestic)"},
    "pct_reports_published_on_time_on_site_foreign": {"short": "Reports on Time (On-site Foreign)", "long": "GMP: % Reports On Time (On-site Foreign)"},
    "pct_reports_published_on_time_reliance_joint_on_site_foreign": {"short": "Reports on Time (Reliance/Joint On-site Foreign)", "long": "GMP: % Reports On Time (Reliance/Joint On-site Foreign)"},
}

# Time-based KPIs (lower values are better)
TIME_BASED: set[str] = {
    "median_duration_continental",
    "avg_turnaround_time",
    "avg_turnaround_time_gmp",
    "median_turnaround_time",
}


def tiny_label(kpi_id: str) -> str:
    """
    Generate a concise, lowercase label for KPI display.

    Args:
        kpi_id (str): KPI identifier.

    Returns:
        str: Simplified label.
    """
    short = KPI_NAME_MAP.get(kpi_id, {}).get("short", kpi_id)
    t = (
        short.replace("on Time", "")
        .replace("Avg ", "Average ")
        .replace("TAT", "turnaround time")
        .strip()
    )
    return t[0].lower() + t[1:] if t else kpi_id


# =======================
# KPI TO PROCESS MAPPING
# =======================
KPI_PROCESS_MAP: Dict[str, str] = {
    # CT KPIs
    "pct_new_apps_evaluated_on_time_ct": "CT",
    "pct_amendment_apps_evaluated_on_time": "CT",
    "pct_gcp_inspections_on_time": "CT",
    "pct_safety_reports_assessed_on_time": "CT",
    "pct_gcp_compliant": "CT",
    "pct_registry_submissions_on_time": "CT",
    "pct_capa_evaluated_on_time": "CT",
    "avg_turnaround_time": "CT",
    # GMP KPIs
    "pct_facilities_inspected_on_time": "GMP",
    "pct_inspections_waived_on_time": "GMP",
    "pct_facilities_compliant": "GMP",
    "pct_capa_decisions_on_time": "GMP",
    "pct_applications_completed_on_time": "GMP",
    "avg_turnaround_time_gmp": "GMP",
    "median_turnaround_time": "GMP",
    "pct_reports_published_on_time": "GMP",
    # MA KPIs
    "pct_new_apps_evaluated_on_time": "MA",
    "pct_renewal_apps_evaluated_on_time": "MA",
    "pct_variation_apps_evaluated_on_time": "MA",
    "pct_fir_responses_on_time": "MA",
    "pct_query_responses_evaluated_on_time": "MA",
    "pct_granted_within_90_days": "MA",
    "median_duration_continental": "MA",
}


# =======================
# DISAGGREGATION FILTERS AND LINKS
# =======================
DISAG_UI_OPTIONS: Dict[str, List[str]] = {
    "MA": ["All"],
    "CT": ["All"],
    "GMP": [
        "All",
        "On-site Domestic",
        "On-site Foreign",
        "Reliance/Joint On-site Foreign",
        "Reliance/Joint Desk-based Foreign",
        "Direct NRA",
        "Reliance Joint",
        "Domestic Applicant",
        "Foreign Direct",
        "Foreign Reliance",
    ],
}

DISAG_KPI_LINKS: Dict[str, Dict[str, str]] = {
    "pct_facilities_inspected_on_time": {
        "On-site Domestic": "pct_facilities_inspected_on_time_on_site_domestic",
        "On-site Foreign": "pct_facilities_inspected_on_time_on_site_foreign",
        "Reliance/Joint On-site Foreign": "pct_facilities_inspected_on_time_reliance_joint_on_site_foreign",
    },
    "pct_facilities_compliant": {
        "On-site Domestic": "pct_facilities_compliant_on_site_domestic",
        "On-site Foreign": "pct_facilities_compliant_on_site_foreign",
        "Reliance/Joint On-site Foreign": "pct_facilities_compliant_reliance_joint_on_site_foreign",
        "Reliance/Joint Desk-based Foreign": "pct_facilities_compliant_reliance_joint_desk_based_foreign",
    },
    "pct_capa_decisions_on_time": {
        "Direct NRA": "pct_capa_decisions_on_time_direct_foreign_domestic_done_by_nra",
        "Reliance Joint": "pct_capa_decisions_on_time_reliance_rec_joint_inspections",
    },
    "pct_applications_completed_on_time": {
        "Domestic Applicant": "pct_applications_completed_on_time_domestic_applicant",
        "Foreign Direct": "pct_applications_completed_on_time_foreign_applicant_direct",
        "Foreign Reliance": "pct_applications_completed_on_time_foreign_applicant_reliance",
    },
    "avg_turnaround_time_gmp": {
        "On-site Domestic": "avg_turnaround_time_gmp_on_site_domestic",
        "On-site Foreign": "avg_turnaround_time_gmp_on_site_foreign",
        "Reliance/Joint On-site Foreign": "avg_turnaround_time_gmp_reliance_joint_on_site_foreign",
    },
    "median_turnaround_time": {
        "On-site Domestic": "median_turnaround_time_on_site_domestic",
        "On-site Foreign": "median_turnaround_time_on_site_foreign",
    },
    "pct_reports_published_on_time": {
        "On-site Domestic": "pct_reports_published_on_time_on_site_domestic",
        "On-site Foreign": "pct_reports_published_on_time_on_site_foreign",
        "Reliance/Joint On-site Foreign": "pct_reports_published_on_time_reliance_joint_on_site_foreign",
    },
}


def has_disag_for_kpi(base_kpi: str, process: str) -> bool:
    """
    Check if a KPI supports disaggregation for the given process.

    Args:
        base_kpi (str): Base KPI ID.
        process (str): Process name (e.g., "GMP").

    Returns:
        bool: True if disaggregation is available.
    """
    return process == "GMP" and base_kpi in DISAG_KPI_LINKS


def resolve_effective_kpi_id(
    base_kpi: str, process: str, disag_choice: str
) -> Tuple[str, Optional[str]]:
    """
    Resolve the effective KPI ID based on disaggregation choice.

    Args:
        base_kpi (str): Base KPI ID.
        process (str): Process name.
        disag_choice (str): Selected disaggregation.

    Returns:
        Tuple[str, Optional[str]]: Effective KPI ID and applied disaggregation name.
    """
    if disag_choice == "All":
        return base_kpi, None
    if process == "GMP" and base_kpi in DISAG_KPI_LINKS:
        target = DISAG_KPI_LINKS[base_kpi].get(disag_choice)
        if target:
            return target, disag_choice
    return base_kpi, None


# =======================
# DATA LOADING
# =======================
@st.cache_data(show_spinner=False)
def load_data(data_path: str) -> Dict[str, Any]:
    """
    Load and validate JSON data from file path.

    Args:
        data_path (str): Path to JSON data file.

    Returns:
        Dict[str, Any]: Loaded and validated data.

    Raises:
        StreamlitError: If file not found or missing required keys.
    """
    p = pathlib.Path(data_path)
    if not p.exists():
        st.error(f"Data file not found: {p}")
        st.stop()
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    required = [
        "quarterlyData",
        "processStepData",
        "kpiCounts",
        "quarterlyVolumes",
        "inspectionVolumes",
        "bottleneckData",
    ]
    for k in required:
        if k not in raw:
            st.error(f"Missing '{k}' in data file.")
            st.stop()
    return raw


# =======================
# UTILITY FUNCTIONS
# =======================
def status_for(kpi_id: str, value: float, target: float) -> str:
    """
    Determine status based on value vs target (success/warning/error).

    Args:
        kpi_id (str): KPI ID.
        value (float): Current value.
        target (float): Target value.

    Returns:
        str: Status ("success", "warning", "error").
    """
    if value is None or target is None:
        return "unknown"
    if kpi_id in TIME_BASED:  # Lower is better for time-based
        if value <= target:
            return "success"
        if value <= target * 1.05:
            return "warning"
        return "error"
    else:  # Higher is better for percentages
        if value >= target:
            return "success"
        if value >= target * 0.95:
            return "warning"
        return "error"


def status_color(status: str) -> str:
    """
    Map status to color.

    Args:
        status (str): Status value.

    Returns:
        str: Hex color.
    """
    return {"success": PALETTE["ok"], "warning": PALETTE["warn"]}.get(status, PALETTE["bad"])


def status_bg_tint(status: str) -> str:
    """
    Map status to background tint.

    Args:
        status (str): Status value.

    Returns:
        str: RGBA background color.
    """
    return {
        "success": "rgba(0, 99, 65, 0.09)",
        "warning": "rgba(245, 158, 11, 0.12)",
        "error": "rgba(198, 40, 40, 0.12)",
    }.get(status, "rgba(0,0,0,0.04)")


def pct(v: Optional[float]) -> Optional[str]:
    """Format value as percentage string."""
    return None if v is None else f"{round(v)}%"



@st.cache_data(show_spinner=False, ttl=300)
def _demo_accounts() -> list:
    """Demonstration sign-ins, read from the local gitignored Terraform output.

    Returns nothing when the file is absent, so a deployed copy of this app
    never displays credentials it does not have.
    """
    path = pathlib.Path(__file__).resolve().parent / "infra" / "generated" / "credentials.json"
    if not path.exists():
        return []
    try:
        generated = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    summary = {
        "data-engineering": "All processes, full dashboard",
        "ma-analysts": "Marketing authorization only",
        "ct-analysts": "Clinical trials only",
        "gmp-analysts": "Manufacturing quality only",
        "public": "2 indicators per process",
    }
    return [
        {"Username": name, "Password": user["password"], "Role": user["group"],
         "Access": summary.get(user["group"], "")}
        for name, user in sorted(generated.get("users", {}).items())
    ]


def export_rights() -> tuple:
    """What the current viewer may export: (allowed, reason, formats).

    An anonymous viewer keeps the dashboard's original behaviour. A signed-in
    stakeholder is held to the entitlement Keycloak and OPA resolved for them,
    so a view-only role sees why the button is absent rather than a dead button.
    """
    rights = st.session_state.get("nda_entitlement")
    if rights is None:
        return True, "", ["csv"]
    if not rights.get("can_export"):
        groups = ", ".join(rights.get("groups", [])) or "your role"
        return False, f"{groups} is view-only — published indicators, no data export.", []
    return True, "", list(rights.get("formats") or ["csv"])


def csv_download(df: pd.DataFrame, filename: str) -> None:
    """Offer this table in every format the viewer is entitled to."""
    allowed, reason, formats = export_rights()
    if not allowed:
        st.caption("Export unavailable — " + reason)
        return
    stem = filename.rsplit(".", 1)[0]
    # A container, not the module: st itself is not a context manager.
    columns = st.columns(len(formats)) if len(formats) > 1 else [st.container()]
    for column, fmt in zip(columns, formats):
        with column:
            if fmt == "xlsx":
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                    df.to_excel(writer, sheet_name=stem[:31] or "data")
                column.download_button(
                    "Download Excel", buffer.getvalue(), file_name=f"{stem}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_xlsx_{stem}")
            else:
                buffer = io.StringIO()
                df.to_csv(buffer, index=True)
                column.download_button(
                    "Download CSV", buffer.getvalue(), file_name=f"{stem}.csv",
                    mime="text/csv", type="primary", key=f"dl_csv_{stem}")


def qp_all() -> dict:
    """
    Get all query parameters (handles API changes).

    Returns:
        dict: Query params as dict.
    """
    try:
        return dict(st.query_params)
    except Exception:
        raw = st.experimental_get_query_params()
        return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}


def qp_get(key: str, default=None) -> Any:
    """
    Get query param by key.

    Args:
        key (str): Param key.
        default: Default value.

    Returns:
        Any: Param value or default.
    """
    return qp_all().get(key, default)


FOCUS_KEY = "focus_kpi"


def init_state(default_kpi: str) -> None:
    """Initialize session state for focused KPI."""
    if FOCUS_KEY not in st.session_state:
        st.session_state[FOCUS_KEY] = qp_get("kpi", None)


def select_kpi(kpi_id: str, process: str, quarter: str) -> None:
    """
    Select a KPI and update query params.

    Args:
        kpi_id (str): KPI ID.
        process (str): Process.
        quarter (str): Quarter.
    """
    mapped_proc = KPI_PROCESS_MAP.get(kpi_id, process)
    st.session_state[FOCUS_KEY] = kpi_id
    try:
        st.query_params.update(kpi=kpi_id, process=mapped_proc, quarter=quarter, tab="Overview")
    except Exception:
        st.experimental_set_query_params(kpi=kpi_id, process=mapped_proc, quarter=quarter, tab="Overview")


def english_summary(counts: Dict[str, int], what: str) -> str:
    """
    Generate natural language summary of status counts.

    Args:
        counts (Dict[str, int]): Status counts.
        what (str): Subject (e.g., "KPIs").

    Returns:
        str: Formatted summary.
    """
    total = sum(counts.values())
    if total == 0:
        return f"No {what.lower()} recorded this quarter."

    ok, warn, bad = (
        counts.get("success", 0),
        counts.get("warning", 0),
        counts.get("error", 0),
    )
    p_ok = (ok / total * 100) if total > 0 else 0
    p_warn = (warn / total * 100) if total > 0 else 0
    p_bad = (bad / total * 100) if total > 0 else 0

    # Determine messaging based on distribution
    if p_bad >= 50:
        statement = "🚨 **Critical Attention Required** - Majority are off track and require immediate intervention to prevent systemic issues."
        nudge = "Focus on root cause analysis and rapid corrective actions for the most critical items first."
    elif p_warn >= 50:
        statement = "⚠️ **Elevated Risk** - Most items are at risk of falling behind targets."
        nudge = "Proactive monitoring and preventive measures needed to stop further deterioration."
    elif p_ok >= 70:
        statement = "✅ **Strong Performance** - Strong compliance with targets across most metrics."
        nudge = "Maintain gains: Please focus on maintaining what works well."
    elif p_ok >= 50:
        statement = "📊 **Moderate Performance** - Meeting targets in key areas with room for improvement."
        nudge = "Focus on converting 'at risk' and 'off-track' items to 'on track' through targeted improvements."
    else:
        if p_bad > p_warn and p_bad > p_ok:
            statement = "🔶 **Mixed Performance Trending Negative** - Performance is fragmented with concerning off-track trends."
            nudge = "Address off-track items immediately while stabilizing at-risk areas."
        else:
            statement = "🔶 **Mixed Performance** - Performance is distributed across all categories without clear dominance."
            nudge = "Root cause analysis needed: Focus on identifying and fixing issues in bottlenecks and process delays."
    return (
        f"{statement}\n\n"
        f"**Breakdown:** {ok}/{total} on track ({p_ok:.0f}%) • {warn}/{total} at risk ({p_warn:.0f}%) • {bad}/{total} off track ({p_bad:.0f}%)\n\n"
        f"**Recommendation:** {nudge}"
    )


# =======================
# PROCESS STEPS AND BOTTLENECKS
# =======================
STEP_ALIASES: Dict[str, str] = {
    "application_submission_review": "Submission review",
    "technical_screening": "Tech screening",
    "committee_assignment": "Committee assign.",
    "committee_review": "Committee review",
    "inspection_scheduling": "Schedule insp.",
    "inspection_execution": "Conduct insp.",
    "report_drafting": "Draft report",
    "report_publication": "Publish report",
    "capa_request": "CAPA request",
    "capa_evaluation": "CAPA evaluation",
}

DISAG_SUFFIXES: List[str] = [
    "_domestic",
    "_foreign",
    "_reliance_joint_on_site_foreign",
    "_reliance_joint_desk_based_foreign",
    "_direct_foreign_domestic_done_by_nra",
    "_reliance_rec_joint_inspections",
    "_domestic_applicant",
    "_foreign_applicant_direct",
    "_foreign_applicant_reliance",
]


def strip_disag_suffix(step_key: str) -> str:
    """
    Remove disaggregation suffix from step key.

    Args:
        step_key (str): Step identifier.

    Returns:
        str: Base step key.
    """
    for suf in DISAG_SUFFIXES:
        if step_key.endswith(suf):
            return step_key[:-len(suf)]
    return step_key


def friendly_step_label(step_key: str) -> str:
    """
    Generate user-friendly label for process step.

    Args:
        step_key (str): Step identifier.

    Returns:
        str: Display label.
    """
    base = strip_disag_suffix(step_key)
    label = STEP_ALIASES.get(base, base.replace("_", " ").title())
    return label


def wrap_label(text: str, max_len: int = 14) -> str:
    """
    Wrap long text into HTML <br> lines.

    Args:
        text (str): Text to wrap.
        max_len (int): Max characters per line.

    Returns:
        str: Wrapped HTML text.
    """
    parts, line, count = [], [], 0
    for word in text.split():
        add = len(word) + (1 if line else 0)
        if count + add > max_len:
            parts.append(" ".join(line))
            line, count = [word], len(word)
        else:
            line.append(word)
            count += add
    if line:
        parts.append(" ".join(line))
    return "<br>".join(parts)


def get_step_status(actual: float, target: float) -> str:
    """
    Determine status for process step duration.

    Args:
        actual (float): Actual days.
        target (float): Target days.

    Returns:
        str: Status ("success", "warning", "error").
    """
    if actual <= target:
        return "success"
    elif actual < target * 1.05:
        return "warning"
    else:
        return "error"


def process_steps_block(
    process: str, quarter: str, processStepData: Dict[str, Any], disag_choice: str
) -> None:
    """
    Render process steps visualization and table.

    Args:
        process (str): Process name.
        quarter (str): Selected quarter.
        processStepData (Dict): Process step data.
        disag_choice (str): Disaggregation choice.
    """
    all_steps = processStepData.get(process, {})
    if not all_steps:
        st.info("No process step data.")
        return

    # Map UI labels to suffixes for filtering
    label2suffix = {
        "On-site Domestic": "_domestic",
        "On-site Foreign": "_foreign",
        "Reliance/Joint On-site Foreign": "_reliance_joint_on_site_foreign",
        "Reliance/Joint Desk-based Foreign": "_reliance_joint_desk_based_foreign",
        "Direct NRA": "_direct_foreign_domestic_done_by_nra",
        "Reliance Joint": "_reliance_rec_joint_inspections",
        "Domestic Applicant": "_domestic_applicant",
        "Foreign Direct": "_foreign_applicant_direct",
        "Foreign Reliance": "_foreign_applicant_reliance",
    }

    # Filter steps based on disaggregation
    if disag_choice == "All":
        steps_dict = {
            k: v for k, v in all_steps.items() if not any(k.endswith(s) for s in DISAG_SUFFIXES)
        }
    else:
        suf = label2suffix.get(disag_choice)
        if suf:
            steps_dict = {k: v for k, v in all_steps.items() if k.endswith(suf)}
            if not steps_dict:
                st.warning("No disag-specific step data found — showing general steps.")
                steps_dict = {
                    k: v
                    for k, v in all_steps.items()
                    if not any(k.endswith(s) for s in DISAG_SUFFIXES)
                }
        else:
            steps_dict = {
                k: v for k, v in all_steps.items() if not any(k.endswith(s) for s in DISAG_SUFFIXES)
            }

    # Build rows for DataFrame
    rows = []
    for step_key, step_obj in steps_dict.items():
        series = step_obj["data"]
        cur = next((x for x in series if x["quarter"] == quarter), None)
        if not cur:
            continue
        metric = cur.get("avgDays")
        target = cur.get("targetDays")
        if metric is None or target is None:
            continue
        label = wrap_label(friendly_step_label(step_key), max_len=16)
        status = get_step_status(float(metric), float(target))
        rows.append(
            {"step": label, "Actual": float(metric), "Target": float(target), "status": status}
        )

    if not rows:
        st.info("No process step rows for this selection.")
        return

    df_bar = pd.DataFrame(rows)

    # Render bar chart
    fig = go.Figure()
    status_colors = {"success": NDA_GREEN, "warning": PALETTE["warn"], "error": PALETTE["bad"]}
    actual_colors = [status_colors[row["status"]] for _, row in df_bar.iterrows()]
    fig.add_trace(
        go.Bar(
            x=df_bar["step"],
            y=df_bar["Actual"],
            name="Actual",
            marker_color=actual_colors,
            text=[f"{v:.0f}d" for v in df_bar["Actual"]],
            textposition="outside",
            textfont=dict(size=12, color=TEXT_DARK),
            legendgroup="Actual",
            hovertemplate="<b>%{x}</b><br>Actual: %{y:.0f} days<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=df_bar["step"],
            y=df_bar["Target"],
            name="Target",
            marker_color=PALETTE["grey"],
            marker_opacity=0.7,
            text=[f"{v:.0f}d" for v in df_bar["Target"]],
            textposition="outside",
            textfont=dict(size=12, color=TEXT_DARK),
            legendgroup="Target",
            hovertemplate="<b>%{x}</b><br>Target: %{y:.0f} days<extra></extra>",
        )
    )
    fig.update_layout(
        barmode="group",
        height=400,
        margin=dict(l=10, r=10, t=10, b=100),
        xaxis_tickangle=-45,
        plot_bgcolor=CARD_BG,
        paper_bgcolor=CARD_BG,
        font=dict(color=TEXT_DARK, size=12),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=-0.3,
            xanchor="center",
            x=0.5,
            bgcolor="rgba(255,255,255,0.8)",
            bordercolor=BORDER_COLOR,
            borderwidth=1,
        ),
        hoverlabel=dict(bgcolor="white", font_size=12, font_family="Inter"),
    )

    st.plotly_chart(fig, use_container_width=True)

    # Render styled table
    df_table = (
        df_bar.assign(Step=lambda d: d["step"].str.replace("<br>", " ", regex=False))
        .drop(columns=["step"])
        .sort_values("Step")
    )

    def apply_row_styling(row):
        styles = [""] * len(df_table.columns)
        actual_idx = df_table.columns.get_loc("Actual")
        color_map = {"success": "#dcfce7", "warning": "#fef3c7", "error": "#fecaca"}
        styles[actual_idx] = f"background-color: {color_map.get(row['status'], 'white')}; border-radius: 4px;"
        return styles

    styled_df = df_table.style.apply(apply_row_styling, axis=1).hide(subset=["status"], axis=1)
    st.dataframe(styled_df, use_container_width=True, hide_index=True)

    # Download option
    csv_download(
        df_table.drop(columns=["status"]),
        f"process_steps_{process}_{quarter}_{disag_choice.replace(' ', '_').lower()}.csv",
    )


# =======================
# CONTEXT CHARTS HELPERS (VOLUME COMPARISONS)
# =======================
def seeded_rng(*parts) -> random.Random:
    """
    Create seeded random number generator for reproducible simulations.

    Args:
        *parts: Seed components.

    Returns:
        random.Random: Seeded RNG.
    """
    s = "|".join(str(p) for p in parts)
    r = random.Random()
    r.seed(s)
    return r


def clamp(v: float, lo: float, hi: float) -> float:
    """Clamp value between lo and hi."""
    return max(lo, min(hi, v))


def build_kpi_comparison_df(
    process: str, kpi_id: str, quarter: str, data: Dict[str, Any]
) -> Tuple[pd.DataFrame, str, str]:
    """
    Build DataFrame for KPI volume comparison chart.

    Args:
        process (str): Process.
        kpi_id (str): KPI ID.
        quarter (str): Quarter (used for year).
        data (Dict): Loaded data.

    Returns:
        Tuple[pd.DataFrame, str, str]: DF, title, ylabel.
    """
    if kpi_id in TIME_BASED:
        return pd.DataFrame(columns=["quarter", "series", "value"]), "", ""

    year = int(quarter.split()[-1])

    def labels_from(block_list: List[Dict]) -> List[str]:
        return [d["quarter"] for d in block_list]

    if process in ["MA", "CT"]:
        qlist = data["quarterlyVolumes"][process]
        year_quarters = sorted(
            [q for q in labels_from(qlist) if int(q.split()[-1]) == year],
            key=lambda s: int(s.split()[0][1:]),
        )
        rec_map = {d["quarter"]: d for d in qlist if d["quarter"] in year_quarters}
    else:
        qlist = data["inspectionVolumes"]["GMP"]
        year_quarters = sorted(
            [q for q in labels_from(qlist) if int(q.split()[-1]) == year],
            key=lambda s: int(s.split()[0][1:]),
        )
        rec_map = {d["quarter"]: d for d in qlist if d["quarter"] in year_quarters}

    if not year_quarters:
        return (
            pd.DataFrame(columns=["quarter", "series", "value"]),
            f"No volume data for {year}",
            "",
        )

    rows = []
    title = ""
    ylabel = "count"

    if process == "MA":
        title_map = {
            "pct_new_apps_evaluated_on_time": "New Applications: Submitted vs Evaluated",
            "pct_renewal_apps_evaluated_on_time": "Renewal Applications: Submitted vs Evaluated",
            "pct_variation_apps_evaluated_on_time": "Variation Applications: Submitted vs Evaluated",
            "pct_fir_responses_on_time": "FIR: Queries vs Responses",
            "pct_query_responses_evaluated_on_time": "Queries: Raised vs Responses",
            "pct_granted_within_90_days": "MA Applications: Submitted vs Granted",
        }
        title = f"{title_map.get(kpi_id, 'MA comparison')} — {year}"
        for q in year_quarters:
            rec = rec_map.get(q, {})
            recvd = int(rec.get("applications_received", 0) or 0)
            compl = int(rec.get("applications_completed", 0) or 0)
            appr = int(rec.get("approvals_granted", 0) or 0)
            rng = seeded_rng("MA", kpi_id, q)
            new_ratio = 0.50 + rng.uniform(-0.05, 0.05)
            ren_ratio = 0.30 + rng.uniform(-0.05, 0.05)
            var_subm = clamp(recvd - int(round(recvd * new_ratio)) - int(round(recvd * ren_ratio)), 0, recvd)
            if kpi_id == "pct_new_apps_evaluated_on_time":
                new_sub = int(round(recvd * new_ratio))
                new_eval = int(round(compl * new_ratio))
                rows += [
                    {"quarter": q, "series": "Submitted", "value": new_sub},
                    {"quarter": q, "series": "Evaluated", "value": new_eval},
                ]
            elif kpi_id == "pct_renewal_apps_evaluated_on_time":
                ren_sub = int(round(recvd * ren_ratio))
                ren_eval = int(round(compl * ren_ratio))
                rows += [
                    {"quarter": q, "series": "Submitted", "value": ren_sub},
                    {"quarter": q, "series": "Evaluated", "value": ren_eval},
                ]
            elif kpi_id == "pct_variation_apps_evaluated_on_time":
                var_sub = var_subm
                var_eval = clamp(compl - int(round(compl * new_ratio)) - int(round(compl * ren_ratio)), 0, compl)
                rows += [
                    {"quarter": q, "series": "Submitted", "value": var_sub},
                    {"quarter": q, "series": "Evaluated", "value": var_eval},
                ]
            elif kpi_id == "pct_fir_responses_on_time":
                fir_q = int(round(compl * clamp(0.35 + rng.uniform(-0.08, 0.08), 0.15, 0.6)))
                fir_r = int(round(fir_q * clamp(0.88 + rng.uniform(-0.05, 0.05), 0.6, 1.0)))
                rows += [
                    {"quarter": q, "series": "FIR queries", "value": fir_q},
                    {"quarter": q, "series": "FIR responses", "value": fir_r},
                ]
            elif kpi_id == "pct_query_responses_evaluated_on_time":
                queries = int(round(compl * clamp(0.55 + rng.uniform(-0.1, 0.1), 0.3, 0.8)))
                q_resps = int(round(queries * clamp(0.82 + rng.uniform(-0.08, 0.08), 0.5, 0.98)))
                rows += [
                    {"quarter": q, "series": "Queries", "value": queries},
                    {"quarter": q, "series": "Query responses", "value": q_resps},
                ]
            elif kpi_id == "pct_granted_within_90_days":
                rows += [
                    {"quarter": q, "series": "Submitted", "value": recvd},
                    {"quarter": q, "series": "Granted", "value": appr},
                ]

    elif process == "CT":
        title_map = {
            "pct_new_apps_evaluated_on_time_ct": "CT New Applications: Submitted vs Evaluated",
            "pct_amendment_apps_evaluated_on_time": "CT Amendments: Submitted vs Evaluated",
            "pct_gcp_inspections_on_time": "GCP Inspections: Planned vs Conducted",
            "pct_safety_reports_assessed_on_time": "Safety Reports: Submitted vs Assessed",
            "pct_gcp_compliant": "GCP Sites: Assessed vs Compliant",
            "pct_registry_submissions_on_time": "Registry: Total reports vs Published",
            "pct_capa_evaluated_on_time": "CAPA: Raised vs Evaluated",
        }
        title = f"{title_map.get(kpi_id, 'CT comparison')} — {year}"
        for q in year_quarters:
            rec = rec_map.get(q, {})
            recvd = int(rec.get("applications_received", 0) or 0)
            compl = int(rec.get("applications_completed", 0) or 0)
            req_insp = int(rec.get("gcp_inspections_requested", 0) or 0)
            cond_insp = int(rec.get("gcp_inspections_conducted", 0) or 0)
            rng = seeded_rng("CT", kpi_id, q)
            new_ratio = 0.65 + rng.uniform(-0.07, 0.07)
            new_subm = int(round(recvd * new_ratio))
            amd_subm = max(recvd - new_subm, 0)
            new_eval = int(
                round(compl * max(min(new_ratio + rng.uniform(-0.03, 0.03), 0.85), 0.4))
            )
            amd_eval = max(compl - new_eval, 0)
            safety_reports = int(
                round(compl * max(min(0.60 + rng.uniform(-0.1, 0.1), 0.9), 0.3))
            )
            safety_assessed = int(
                round(safety_reports * max(min(0.9 + rng.uniform(-0.08, 0.05), 1.0), 0.5))
            )
            sites_assessed = int(
                round(cond_insp * max(min(1.2 + rng.uniform(-0.2, 0.2), 2.0), 0.5))
            )
            sites_compliant = int(
                round(sites_assessed * max(min(0.9 + rng.uniform(-0.05, 0.05), 1.0), 0.6))
            )
            registry_sub = int(
                round(recvd * max(min(0.5 + rng.uniform(-0.1, 0.1), 0.9), 0.3))
            )
            registry_proc = int(
                round(registry_sub * max(min(0.9 + rng.uniform(-0.05, 0.05), 1.0), 0.6))
            )
            capa_raised = int(
                round(compl * max(min(0.25 + rng.uniform(-0.08, 0.08), 0.6), 0.1))
            )
            capa_eval = int(
                round(capa_raised * max(min(0.9 + rng.uniform(-0.08, 0.05), 1.0), 0.5))
            )
            if kpi_id == "pct_new_apps_evaluated_on_time_ct":
                rows += [
                    {"quarter": q, "series": "Submitted", "value": new_subm},
                    {"quarter": q, "series": "Evaluated", "value": new_eval},
                ]
            elif kpi_id == "pct_amendment_apps_evaluated_on_time":
                rows += [
                    {"quarter": q, "series": "Submitted", "value": amd_subm},
                    {"quarter": q, "series": "Evaluated", "value": amd_eval},
                ]
            elif kpi_id == "pct_gcp_inspections_on_time":
                rows += [
                    {"quarter": q, "series": "Planned", "value": req_insp},
                    {"quarter": q, "series": "Conducted", "value": cond_insp},
                ]
            elif kpi_id == "pct_safety_reports_assessed_on_time":
                rows += [
                    {"quarter": q, "series": "Safety reports", "value": safety_reports},
                    {"quarter": q, "series": "Assessed", "value": safety_assessed},
                ]
            elif kpi_id == "pct_gcp_compliant":
                rows += [
                    {"quarter": q, "series": "Sites assessed", "value": sites_assessed},
                    {"quarter": q, "series": "Compliant", "value": sites_compliant},
                ]
            elif kpi_id == "pct_registry_submissions_on_time":
                rows += [
                    {"quarter": q, "series": "Total reports", "value": registry_sub},
                    {"quarter": q, "series": "Published", "value": registry_proc},
                ]
            elif kpi_id == "pct_capa_evaluated_on_time":
                rows += [
                    {"quarter": q, "series": "CAPA raised", "value": capa_raised},
                    {"quarter": q, "series": "Evaluated", "value": capa_eval},
                ]

    elif process == "GMP":
        title_map = {
            "pct_facilities_inspected_on_time": "GMP: Submitted vs Inspected by Inspection Type",
            "pct_inspections_waived_on_time": "GMP: Total Inspections vs Waived (Desk/Remote)",
            "pct_facilities_compliant": "GMP: Conducted vs Compliant by Inspection Type",
            "pct_capa_decisions_on_time": "GMP: CAPA Decisions by Inspection Source",
            "pct_applications_completed_on_time": "GMP: Applications by Source",
            "pct_reports_published_on_time": "GMP: Reports Published by Inspection Type",
        }
        title = f"{title_map.get(kpi_id, 'GMP comparison')} — {year}"
        for q in year_quarters:
            rec = rec_map.get(q, {})
            rng = seeded_rng("GMP", kpi_id, q)
            req = {
                "Domestic": int(rec.get("requested_domestic", 0) or 0),
                "Foreign": int(rec.get("requested_foreign", 0) or 0),
                "Reliance": int(rec.get("requested_reliance", 0) or 0),
                "Desk": int(rec.get("requested_desk", 0) or 0),
            }
            cond = {
                "Domestic": int(rec.get("conducted_domestic", 0) or 0),
                "Foreign": int(rec.get("conducted_foreign", 0) or 0),
                "Reliance": int(rec.get("conducted_reliance", 0) or 0),
                "Desk": int(rec.get("conducted_desk", 0) or 0),
            }
            types = ["Domestic", "Foreign", "Reliance", "Desk"]
            waived = {
                t: int(round(req[t] * clamp(0.12 + rng.uniform(-0.05, 0.05), 0, 0.3)))
                for t in types
            }
            compliant = {
                t: int(round(cond[t] * clamp(0.88 + rng.uniform(-0.06, 0.05), 0.5, 1.0)))
                for t in types
            }
            capa = {
                t: int(round(cond[t] * clamp(0.30 + rng.uniform(-0.1, 0.1), 0.05, 0.7)))
                for t in types
            }
            apps_by_src = {
                t: int(round(req[t] * clamp(1.10 + rng.uniform(-0.2, 0.2), 0.4, 2.0)))
                for t in types
            }
            reports = {
                t: int(round(cond[t] * clamp(0.95 + rng.uniform(-0.05, 0.05), 0.5, 1.2)))
                for t in types
            }
            if kpi_id == "pct_facilities_inspected_on_time":
                for t in types:
                    rows += [
                        {"quarter": q, "series": f"{t} — Submitted", "value": req[t]},
                        {"quarter": q, "series": f"{t} — Inspected", "value": cond[t]},
                    ]
            elif kpi_id == "pct_inspections_waived_on_time":
                total_inspections = sum(req.values())
                total_waived = waived["Desk"]
                rows += [
                    {"quarter": q, "series": "Total Inspections", "value": total_inspections},
                    {"quarter": q, "series": "Waived (Desk/Remote)", "value": total_waived},
                ]
            elif kpi_id == "pct_facilities_compliant":
                for t in types:
                    rows += [
                        {"quarter": q, "series": f"{t} — Conducted", "value": cond[t]},
                        {"quarter": q, "series": f"{t} — Compliant", "value": compliant[t]},
                    ]
            elif kpi_id == "pct_capa_decisions_on_time":
                for t in ["Domestic", "Foreign", "Reliance"]:
                    rows += [
                        {"quarter": q, "series": f"{t} — CAPA decisions", "value": capa[t]},
                    ]
            elif kpi_id == "pct_applications_completed_on_time":
                for t in ["Domestic", "Foreign", "Reliance"]:
                    rows += [
                        {"quarter": q, "series": f"{t} — Applications", "value": apps_by_src[t]},
                    ]
            elif kpi_id == "pct_reports_published_on_time":
                for t in types:
                    rows += [
                        {"quarter": q, "series": f"{t} — Reports published", "value": reports[t]},
                    ]

    df = pd.DataFrame(rows)
    return df, title, ylabel


def _pair_spec_for_kpi(process: str, kpi_id: str) -> Optional[Tuple[Optional[str], str, List[str]]]:
    """
    Get pair specification for volume comparison.

    Args:
        process (str): Process.
        kpi_id (str): KPI ID.

    Returns:
        Optional[Tuple]: Base label, compare label, group levels.
    """
    if process == "MA":
        pairs = {
            "pct_new_apps_evaluated_on_time": ("Submitted", "Evaluated", ["All"]),
            "pct_renewal_apps_evaluated_on_time": ("Submitted", "Evaluated", ["All"]),
            "pct_variation_apps_evaluated_on_time": ("Submitted", "Evaluated", ["All"]),
            "pct_fir_responses_on_time": ("FIR queries", "FIR responses", ["All"]),
            "pct_query_responses_evaluated_on_time": ("Queries", "Query responses", ["All"]),
            "pct_granted_within_90_days": ("Submitted", "Granted", ["All"]),
        }
        return pairs.get(kpi_id)
    if process == "CT":
        pairs = {
            "pct_new_apps_evaluated_on_time_ct": ("Submitted", "Evaluated", ["All"]),
            "pct_amendment_apps_evaluated_on_time": ("Submitted", "Evaluated", ["All"]),
            "pct_gcp_inspections_on_time": ("Planned", "Conducted", ["All"]),
            "pct_safety_reports_assessed_on_time": ("Safety reports", "Assessed", ["All"]),
            "pct_gcp_compliant": ("Sites assessed", "Compliant", ["All"]),
            "pct_registry_submissions_on_time": ("Total reports", "Published", ["All"]),
            "pct_capa_evaluated_on_time": ("CAPA raised", "Evaluated", ["All"]),
        }
        return pairs.get(kpi_id)
    if process == "GMP":
        pairs = {
            "pct_facilities_inspected_on_time": (
                "Submitted",
                "Inspected",
                ["Domestic", "Foreign", "Reliance", "Desk"],
            ),
            "pct_facilities_compliant": (
                "Conducted",
                "Compliant",
                ["Domestic", "Foreign", "Reliance", "Desk"],
            ),
            "pct_inspections_waived_on_time": ("Total Inspections", "Waived (Desk/Remote)", ["All"]),
            "pct_capa_decisions_on_time": (None, "CAPA decisions", ["Domestic", "Foreign", "Reliance"]),
            "pct_applications_completed_on_time": (None, "Applications", ["Domestic", "Foreign", "Reliance"]),
            "pct_reports_published_on_time": (
                "Conducted",
                "Reports published",
                ["Domestic", "Foreign", "Reliance", "Desk"],
            ),
        }
        return pairs.get(kpi_id)
    return None


def _prepare_category_first_df(
    process: str, kpi_id: str, quarter: str, data: Dict[str, Any]
) -> Tuple[pd.DataFrame, str, List[str], List[str]]:
    """
    Prepare DataFrame for category-first volume analysis.

    Args:
        process (str): Process.
        kpi_id (str): KPI ID.
        quarter (str): Quarter.
        data (Dict): Data.

    Returns:
        Tuple: DF, title, categories, group levels.
    """
    spec = _pair_spec_for_kpi(process, kpi_id)
    if not spec:
        return pd.DataFrame(), "", [], []
    base_label, compare_label, group_levels = spec
    df_raw, title, _ = build_kpi_comparison_df(process, kpi_id, quarter, data)
    if df_raw.empty:
        return pd.DataFrame(), title, [], []

    rows = []
    for _, r in df_raw.iterrows():
        series = str(r["series"])
        if "—" in series:
            group, category = [s.strip() for s in series.split("—", 1)]
        else:
            group, category = "All", series
        rows.append(
            {
                "quarter": r["quarter"],
                "group": group,
                "category": category,
                "value": int(r["value"] or 0),
            }
        )
    d = pd.DataFrame(rows)
    categories = [c for c in [base_label, compare_label] if c is not None] if base_label else [compare_label]
    d = d[d["category"].isin(categories)].copy()
    if base_label is not None and len(group_levels) == 1:
        pair_tot = d.groupby("quarter")["value"].sum().rename("pair_total")
        d = d.merge(pair_tot.reset_index(), on="quarter", how="left")
        d["pct"] = (d["value"] / d["pair_total"]) * 100.0
        if kpi_id == "pct_inspections_waived_on_time":
            total_vals = d[d["category"] == "Total Inspections"].set_index("quarter")["value"]
            waived_rows = d["category"] == "Waived (Desk/Remote)"
            d.loc[waived_rows, "pct"] = (
                d.loc[waived_rows, "value"] / total_vals[d.loc[waived_rows, "quarter"]].values
            ) * 100
            d.loc[d["category"] == "Total Inspections", "pct"] = 100.0
    else:
        cat_tot = (
            d.groupby(["quarter", "category"], as_index=False)["value"].sum().rename(columns={"value": "cat_total"})
        )
        d = d.merge(cat_tot, on=["quarter", "category"], how="left")
        d["pct"] = np.where(d["cat_total"] > 0, (d["value"] / d["cat_total"]) * 100.0, np.nan)
    if process == "GMP":
        d["group"] = pd.Categorical(d["group"], categories=group_levels, ordered=True)
    else:
        d["group"] = pd.Categorical(d["group"], categories=["All"], ordered=True)
    return d, title, categories, group_levels


def render_kpi_comparison(process: str, kpi_id: str, quarter: str, data: Dict[str, Any]) -> None:
    """
    Render volume comparison chart for KPI.

    Args:
        process (str): Process.
        kpi_id (str): KPI ID.
        quarter (str): Quarter.
        data (Dict): Data.
    """
    if data.get("_meta", {}).get("source"):
        observed = next((r for r in data.get("kpiCounts", {}).get(process, {}).get(kpi_id, []) if r["quarter"] == quarter), None)
        if not observed:
            st.info("No completed observations for this indicator and quarter.")
        elif "sample_n" in observed:
            st.metric("Completed observations", observed["sample_n"])
        else:
            fig = go.Figure(go.Bar(x=["Meeting criterion", "Not meeting criterion"], y=[observed["numerator"], observed["denominator"]-observed["numerator"]], marker_color=[NDA_GREEN, "#D96957"]))
            fig.update_layout(height=300, yaxis_title="Completed activity observations")
            st.plotly_chart(fig, use_container_width=True)
        return
    d, title, categories, group_levels = _prepare_category_first_df(process, kpi_id, quarter, data)
    if d.empty or not title:
        st.info("No per-quarter comparison chart for this KPI.")
        return

    fig = go.Figure()
    if process in ("MA", "CT"):
        qorder = sorted(
            d["quarter"].unique(), key=lambda s: (int(s.split()[1]), int(s.split()[0][1:]))
        )
        for i, cat in enumerate(categories):
            dd = d[d["category"] == cat].groupby("quarter", as_index=False).agg(
                {"value": "sum", "pct": "mean"}
            )
            dd["quarter"] = pd.Categorical(dd["quarter"], categories=qorder, ordered=True)
            color = NDA_GREEN if i == 0 else NDA_ACCENT
            fig.add_bar(
                x=dd["quarter"],
                y=dd["value"],
                name=cat,
                marker=dict(color=color),
                text=[
                    f"{int(v):,} ({p:.0f}%)" if not np.isnan(p) else f"{int(v):,} (—)"
                    for v, p in zip(dd["value"], dd["pct"])
                ],
                textposition="outside",
            )
        fig.update_layout(
            title=title,
            barmode="group",
            bargap=0.25,
            bargroupgap=0.15,
            margin=dict(l=10, r=10, t=50, b=10),
            plot_bgcolor=CARD_BG,
            paper_bgcolor=CARD_BG,
            font=dict(color=TEXT_DARK),
            legend=dict(orientation="h", y=-0.2),
            xaxis=dict(title=""),
            yaxis=dict(title="count", rangemode="tozero"),
        )
        st.plotly_chart(fig, use_container_width=True)
        return

    def _color_for_group(g: str) -> str:
        return GMP_GROUP_COLORS.get(g, NDA_GREEN)

    qorder = sorted(
        d["quarter"].unique(), key=lambda s: (int(s.split()[1]), int(s.split()[0][1:]))
    )
    x_axis = []
    for q in qorder:
        for cat in categories:
            x_axis.append((q, cat))
    look = {}
    for _, r in d.iterrows():
        look[(str(r["group"]), r["quarter"], r["category"])] = (
            int(r["value"]),
            float(r["pct"]) if not pd.isna(r["pct"]) else np.nan,
        )
    for g in group_levels:
        xs, ys, texts = [], [], []
        for (q, cat) in x_axis:
            v, p = look.get((g, q, cat), (0, np.nan))
            xs.append((q, cat))
            ys.append(v)
            texts.append(
                f"{int(v):,} ({p:.0f}%)" if not np.isnan(p) else f"{int(v):,} (—)"
            )
        fig.add_bar(
            x=xs,
            y=ys,
            name=g,
            marker=dict(color=_color_for_group(g)),
            text=texts,
            textposition="outside",
        )
    fig.update_layout(
        title=title,
        barmode="group",
        bargap=0.25,
        bargroupgap=0.15,
        margin=dict(l=10, r=10, t=50, b=10),
        plot_bgcolor=CARD_BG,
        paper_bgcolor=CARD_BG,
        font=dict(color=TEXT_DARK),
        legend=dict(orientation="h", y=-0.2),
        xaxis=dict(title="", type="category"),
        yaxis=dict(title="count", rangemode="tozero"),
    )
    st.plotly_chart(fig, use_container_width=True)


# =======================
# KPI TREND VISUALIZATION
# =======================
def kpi_trend(
    process: str,
    base_kpi_id: str,
    kpis_block: Dict[str, Any],
    quarter: str,
    disag_choice: str,
) -> None:
    """
    Render trend line chart for KPI, respecting disaggregation.

    Args:
        process (str): Process.
        base_kpi_id (str): Base KPI ID.
        kpis_block (Dict): KPIs data.
        quarter (str): Selected quarter.
        disag_choice (str): Disaggregation.
    """
    # Special handling for GMP all-disag view
    if (
        process == "GMP"
        and base_kpi_id
        in [
            "pct_facilities_inspected_on_time",
            "pct_facilities_compliant",
            "pct_capa_decisions_on_time",
            "pct_applications_completed_on_time",
            "pct_reports_published_on_time",
        ]
        and disag_choice == "All"
    ):
        fig = go.Figure()
        child_map = DISAG_KPI_LINKS.get(base_kpi_id, {})
        ref_quarters = None
        for label, kid in child_map.items():
            k = kpis_block.get(kid)
            if not k:
                continue
            series = pd.DataFrame(k["data"])
            if ref_quarters is None:
                ref_quarters = series["quarter"].tolist()
            fig.add_trace(
                go.Scatter(
                    x=series["quarter"],
                    y=series["value"],
                    name=label,
                    mode="lines+markers",
                )
            )
        k_base = kpis_block.get(base_kpi_id)
        if k_base:
            series_base = pd.DataFrame(k_base["data"])
            if ref_quarters is None:
                ref_quarters = series_base["quarter"].tolist()
            fig.add_trace(
                go.Scatter(
                    x=series_base["quarter"],
                    y=series_base["value"],
                    name="Overall",
                    mode="lines+markers",
                    line=dict(width=4, color=NDA_GREEN),
                )
            )
            target = k_base.get("target")
            baseline = k_base.get("baseline")
            if target is not None:
                fig.add_trace(
                    go.Scatter(
                        x=ref_quarters,
                        y=[target] * len(ref_quarters),
                        name="Target",
                        mode="lines",
                        line=dict(dash="dash", color=NDA_ACCENT),
                    )
                )
            if baseline is not None:
                fig.add_trace(
                    go.Scatter(
                        x=ref_quarters,
                        y=[baseline] * len(ref_quarters),
                        name="Baseline",
                        mode="lines",
                        line=dict(dash="dot", color="#94a3b8"),
                    )
                )
        y_max = 100 if base_kpi_id.startswith("pct_") else None
        fig.update_layout(
            title="Trend vs Target — All disaggregations",
            height=500,
            margin=dict(l=10, r=10, t=40, b=0),
            yaxis_range=[0, y_max] if y_max else None,
            plot_bgcolor=CARD_BG,
            paper_bgcolor=CARD_BG,
            font=dict(color=TEXT_DARK),
            legend=dict(orientation="h", y=-0.2),
        )
        st.plotly_chart(fig, use_container_width=True)
        return

    # Standard trend for effective KPI
    effective_kpi_id, applied = resolve_effective_kpi_id(base_kpi_id, process, disag_choice)
    k = kpis_block.get(effective_kpi_id) or kpis_block.get(base_kpi_id)
    if not k:
        st.warning("No KPI series found.")
        return
    series = pd.DataFrame(k["data"])
    target = k.get("target")
    baseline = k.get("baseline")
    y_max = 100 if effective_kpi_id.startswith("pct_") else None
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=series["quarter"],
            y=series["value"],
            name="Performance",
            mode="lines+markers",
            line=dict(width=3, color=NDA_GREEN),
        )
    )
    if target is not None:
        fig.add_trace(
            go.Scatter(
                x=series["quarter"],
                y=[target] * len(series),
                name="Target",
                mode="lines",
                line=dict(dash="dash", color=NDA_ACCENT),
            )
        )
    if baseline is not None:
        fig.add_trace(
            go.Scatter(
                x=series["quarter"],
                y=[baseline] * len(series),
                name="Baseline",
                mode="lines",
                line=dict(dash="dot", color="#94a3b8"),
            )
        )
    title_suffix = f" — {applied}" if applied else ""
    fig.update_layout(
        title=f"Trend vs Target{title_suffix}",
        height=500,
        margin=dict(l=10, r=10, t=40, b=0),
        yaxis_range=[0, y_max] if y_max else None,
        plot_bgcolor=CARD_BG,
        paper_bgcolor=CARD_BG,
        font=dict(color=TEXT_DARK),
        legend=dict(orientation="h", y=-0.2),
    )
    st.plotly_chart(fig, use_container_width=True)


# =======================
# KPI CARD COMPONENT
# =======================
def kpi_card(
    kpi_id: str, kpi_obj: Dict[str, Any], quarter: str, *, process: str
) -> bool:
    """
    Render interactive KPI card.

    Args:
        kpi_id (str): KPI ID.
        kpi_obj (Dict): KPI data.
        quarter (str): Quarter.
        process (str): Process.

    Returns:
        bool: True if details button clicked.
    """
    series = kpi_obj["data"]
    cur = next((x for x in series if x["quarter"] == quarter), None)
    prev_val = None
    if cur:
        idx = series.index(cur)
        if idx > 0:
            prev_val = series[idx - 1]["value"]
    is_time = kpi_id in TIME_BASED
    is_pct = kpi_id.startswith("pct_")
    delta = None if (not cur or prev_val is None) else (cur["value"] - prev_val)
    vdisp = (
        pct(cur["value"])
        if (cur and is_pct)
        else (f"{cur['value']:.2f}" if cur else "—")
    )
    ddisp = (
        None
        if delta is None
        else (f"{'+' if delta > 0 else ''}{delta:.1f}" + (" pp" if is_pct else ""))
    )
    good_vs_prev = (delta is not None) and ((delta < 0) if is_time else (delta > 0))
    status = status_for(
        kpi_id, None if not cur else cur["value"], kpi_obj.get("target")
    )
    bleft = status_color(status)
    btint = status_bg_tint(status)
    status_label = {
        "success": "On target",
        "warning": "Near target",
        "error": "Below target",
    }.get(status, "—")
    short = KPI_NAME_MAP.get(kpi_id, {}).get("short", kpi_id)
    unit = "days" if is_time else ""
    chips = []
    if ddisp:
        chips.append(f"<span class='kpi-chip {'neutral' if delta == 0 else ('ok' if good_vs_prev else 'bad')}'>{ddisp} vs previous quarter</span>")
    chips.append(f"<span class='kpi-chip' style='color:{bleft};background:{btint}'>{status_label}</span>")
    target = kpi_obj.get("target")
    target_text = pct(target) if is_pct else (f"{target:g} {unit}" if isinstance(target, (int, float)) else "—")
    with st.container(border=True, key=f"indicator_{process}_{kpi_id}"):
        st.markdown(
            f"<div class='kpi-topline' style='--accent:{bleft}'></div>"
            f"<div class='kpi-title'>{escape(short)}</div>"
            f"<div class='kpi-value'>{vdisp}<span class='kpi-unit'>{unit}</span></div>"
            + " ".join(chips)
            + f"<div class='kpi-sub'>Target: {target_text}<br>{escape(tiny_label(kpi_id))}</div>",
            unsafe_allow_html=True,
        )
        # A compact history uses only observations up to the selected quarter.
        history = series[:series.index(cur) + 1] if cur else []
        points = [item for item in history if isinstance(item.get("value"), (int, float))]
        if len(points) > 1:
            values = [item["value"] for item in points]
            low, high = min(values), max(values)
            span = high - low or 1
            coords = [(4 + i * 292 / (len(values) - 1), 43 - (value - low) / span * 32) for i, value in enumerate(values)]
            line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            area = f"4,52 {line} 296,52"
            st.html(
                f'<svg class="sparkline" viewBox="0 0 300 54" preserveAspectRatio="none" role="img" aria-label="Historical indicator trend">'
                f'<polygon points="{area}" fill="{bleft}" opacity="0.09" />'
                f'<polyline points="{line}" fill="none" stroke="{bleft}" stroke-width="2.5" stroke-linejoin="round" />'
                f'<circle cx="{coords[-1][0]}" cy="{coords[-1][1]}" r="3.5" fill="{bleft}" /></svg>'
                f'<div class="sparkline-labels"><span>{escape(points[0]["quarter"])}</span><span>{escape(points[-1]["quarter"])}</span></div>'
            )
        clicked = st.button("Explore indicator →", key=f"kbtn_{process}_{kpi_id}_{quarter}", use_container_width=True)
    return clicked


# =======================
# APPLICATION HEADER
# =======================
st.markdown(
    """
<div class="header">
  <div class="hero-copy">
    <div class="eyebrow">National Drug Authority &middot; Uganda</div>
    <h1>Better regulation.<br><span>Measurable impact.</span></h1>
    <p class="subtitle">A clear view of regulatory performance. Track progress, uncover bottlenecks and turn evidence into better decisions.</p>
    <div class="hero-tags"><span>Marketing authorization</span><span>Clinical trials</span><span>Manufacturing quality</span></div>
  </div>
  <div class="hero-art" aria-hidden="true"><div class="orbit"></div><div class="art-label one">REGULATORY INTELLIGENCE</div><div class="art-label two">EVIDENCE INTO ACTION &#8599;</div></div>
</div>
""",
    unsafe_allow_html=True,
)


# =======================
# SIDEBAR CONFIGURATION
# =======================
st.sidebar.image("logo.jpg", width=150)
st.sidebar.markdown('<div class="sidebar-eyebrow">Explore the data</div>', unsafe_allow_html=True)
with st.sidebar.expander("Data source", expanded=False):
    # Default to the live analytics engine whenever its credentials are configured.
    # Default to the role-gated view when identity is configured: this is an
    # organizational dashboard, so who you are decides what it shows.
    _access_configured = (pathlib.Path(__file__).resolve().parent
                          / "infra" / "generated" / "credentials.json").exists()
    _default_source = 2 if _access_configured else (1 if os.environ.get("NDA_API_KEY") else 0)
    source_mode = st.radio("Source", ["Reference data", "Live application stream", "Sign in"],
                           index=_default_source, key="nda_source_mode")
    if source_mode == "Reference data":
        data_path = st.text_input("Data file", value="data/kpiData.json")
    else:
        api_url = st.text_input("API URL", value=os.environ.get("NDA_API_URL", "http://127.0.0.1:8095"))
        auto_refresh = st.checkbox("Auto-refresh", value=False,
                                   help="Figures only change when the lakehouse commits a "
                                        "checkpoint, about once a minute.")

# Signed-in stakeholders get a view cut to their entitlement, resolved by
# Keycloak (identity) and OPA (policy). Nothing here decides access itself.
entitlement = st.session_state.get("nda_entitlement")
if source_mode == "Sign in":
    api_url = os.environ.get("NDA_API_URL", "http://127.0.0.1:8095").rstrip("/")
    # Only shown once signed in; the main-area gate below handles signing in, so
    # the sidebar does not duplicate the same form.
    with st.sidebar.expander("Your account", expanded=False) if entitlement else nullcontext():
        if entitlement:
            who = st.session_state.get("nda_user", {})
            st.markdown(f"**{who.get('name') or who.get('username')}**")
            st.caption(" · ".join(entitlement.get("groups", [])) or "no group")
            if st.button("Sign out", key="nda_signout"):
                for key in ("nda_token", "nda_user", "nda_entitlement"):
                    st.session_state.pop(key, None)
                st.rerun()
    if not st.session_state.get("nda_token"):
        # A proper sign-in gate in the main area. The sidebar form alone was not
        # discoverable, which is the whole point of a login screen.
        st.markdown("## Sign in")
        st.caption("Your role decides which regulatory processes you see and what you may export.")
        gate, side = st.columns([3, 2], gap="large")
        with gate:
            with st.form("nda_signin_main"):
                gate_user = st.text_input("Username", key="nda_username_main",
                                          placeholder="e.g. alice.nakato")
                gate_pass = st.text_input("Password", type="password", key="nda_password_main")
                if st.form_submit_button("Sign in", type="primary") and gate_user:
                    try:
                        response = requests.post(api_url + "/v1/auth/login", timeout=30,
                                                 json={"username": gate_user, "password": gate_pass})
                        if response.status_code == 200:
                            body = response.json()
                            st.session_state["nda_token"] = body["access_token"]
                            st.session_state["nda_user"] = body["user"]
                            st.session_state["nda_entitlement"] = body["entitlement"]
                            st.rerun()
                        else:
                            st.error(response.json().get("detail", "Sign in failed"))
                    except requests.RequestException:
                        st.error("The sign-in service is unavailable.")
        with side:
            st.markdown("##### Access levels")
            levels = [
                "| Role | Sees | Can export |",
                "|---|---|---|",
                "| Data engineering | All three processes | Bronze, silver, gold |",
                "| MA / CT / GMP analyst | Their own process | That process, silver + gold |",
                "| Public | 2 indicators per process | Nothing |",
            ]
            st.markdown(chr(10).join(levels))
        accounts = _demo_accounts()
        if accounts:
            with st.expander("Demonstration accounts", expanded=True):
                st.caption("Local synthetic environment. Read from the gitignored "
                           "infra/generated/credentials.json, never from source control.")
                st.dataframe(pd.DataFrame(accounts), hide_index=True, use_container_width=True)
        st.stop()
    try:
        scoped = requests.get(api_url + "/v1/dashboard/scoped", timeout=120,
                              headers={"Authorization": "Bearer " + st.session_state["nda_token"]})
        if scoped.status_code == 401:
            for key in ("nda_token", "nda_user", "nda_entitlement"):
                st.session_state.pop(key, None)
            st.warning("Your session expired. Sign in again.")
            st.stop()
        scoped.raise_for_status()
        data = scoped.json()
    except requests.RequestException:
        st.error("The live data service is unavailable. Reference data has not been substituted.")
        st.stop()
    entitlement = st.session_state.get("nda_entitlement") or {}
    shown = entitlement.get("indicators")
    st.caption("Signed in as " + (st.session_state["nda_user"].get("name") or "user")
               + " · " + ", ".join(entitlement.get("groups", []))
               + " · processes: " + (", ".join(sorted(entitlement.get("processes", []))) or "none")
               + (" · all indicators" if shown == "*" else f" · {len(shown or [])} indicators"))
elif source_mode == "Reference data":
    data = load_data(data_path)
else:
    try:
        api_response = requests.get(api_url.rstrip("/") + "/v1/dashboard", headers={"X-API-Key": os.environ.get("NDA_API_KEY", "")}, timeout=15)
        api_response.raise_for_status()
        data = api_response.json()
        if not all(k in data for k in ("quarterlyData", "processStepData", "kpiCounts", "quarterlyVolumes", "inspectionVolumes", "bottleneckData")):
            raise ValueError("Incomplete dashboard contract")
    except (requests.RequestException, ValueError):
        st.error("The live data service is unavailable or not configured. Check the API connection and credentials. Reference data has not been substituted.")
        st.stop()
    st.caption("Synthetic application stream · " + data.get("_meta", {}).get("generated_at", "") + " · Figures update after a committed lakehouse checkpoint.")
    # A rerun repaints the whole page, so it is paced to the checkpoint interval
    # rather than to the polling interval - anything faster only made it flicker.
    if auto_refresh:
        @st.fragment(run_every=60)
        def refresh_live_dashboard():
            now = time.monotonic()
            previous = st.session_state.get("nda_last_refresh", now)
            if now - previous >= 59:
                st.session_state["nda_last_refresh"] = now
                st.rerun()
            st.session_state.setdefault("nda_last_refresh", now)
        refresh_live_dashboard()
# The public view is indicator tiles only: operational detail (volumes, workflow
# steps, bottlenecks) is withheld, so the Reports builder has nothing to work on
# and is not offered rather than being shown empty or broken.
_rights = entitlement or {}
_has_operational_detail = bool(_rights.get("full_dashboard") or _rights.get("can_export")) if entitlement else True
views = ["Overview"]
if _has_operational_detail:
    views.append("Reports")
if _rights.get("can_export"):
    views.append("Data downloads")
tab = st.sidebar.radio("View", views, index=0, horizontal=False)

# Extract all available quarters
all_quarters = sorted(
    {
        q
        for proc in data["quarterlyData"].values()
        for k in proc.values()
        for q in [d["quarter"] for d in k["data"]]
    },
    key=lambda s: (int(s.split()[1]), int(s.split()[0][1:])),
)


# =======================
# SELF-SERVICE ANALYTICS PREPARATION
# =======================
if not all_quarters:
    st.info("Waiting for the first completed application activity to reach the KPI tables.")
    st.stop()

def prep_analysis(
    df: pd.DataFrame,
    analysis_type: str,
    processes: List[str],
    metrics: List[str],
    group_by: str,
    agg: str,
    compare_by_category: bool,
    show_pct_change: bool,
    x_metric: Optional[str] = None,
    y_metric: Optional[str] = None,
) -> Tuple[pd.DataFrame, str, Dict[str, Any], str, Optional[pd.DataFrame]]:
    """
    Prepare pivot table for analysis.

    Args:
        df (pd.DataFrame): Input data.
        analysis_type (str): Type ("Trend", "Comparison", etc.).
        processes (List[str]): Processes.
        metrics (List[str]): Metrics.
        group_by (str): Grouping column.
        agg (str): Aggregation function.
        compare_by_category (bool): Compare by category.
        show_pct_change (bool): Show % change.
        x_metric (Optional[str]): X metric for correlation.
        y_metric (Optional[str]): Y metric for correlation.

    Returns:
        Tuple: Pivot DF, agg, metadata, type, % change DF.
    """
    if df.empty:
        return pd.DataFrame(), None, None, None, None
    filtered = df[df["process"].isin(processes)].copy()
    is_time_series = group_by in ["quarter", "year"]
    pt = None
    meta = {"color_var": None, "x_col": None, "y_col": None}
    if analysis_type == "Correlation":
        if not (x_metric and y_metric):
            return pd.DataFrame(), None, None, None, None
        filtered = filtered[filtered["metric_name"].isin([x_metric, y_metric])]
        if filtered.empty:
            return pd.DataFrame(), None, None, None, None
        if group_by not in ["quarter", "year"]:
            group_by = "quarter"
        pt_x = pd.pivot_table(
            filtered[filtered["metric_name"] == x_metric],
            values="value",
            index=group_by,
            aggfunc=agg,
            fill_value=0,
        )
        pt_x.columns = [metric_display_name(x_metric)]
        pt_y = pd.pivot_table(
            filtered[filtered["metric_name"] == y_metric],
            values="value",
            index=group_by,
            aggfunc=agg,
            fill_value=0,
        )
        pt_y.columns = [metric_display_name(y_metric)]
        pt = pt_x.join(pt_y, how="inner").sort_index()
        meta = {"x_col": pt.columns[0], "y_col": pt.columns[1], "color_var": None}
        return pt, agg, meta, "Correlation", None
    if metrics:
        filtered = filtered[filtered["metric_name"].isin(metrics)]
    if filtered.empty:
        return pd.DataFrame(), None, None, None, None
    if compare_by_category and "category" in filtered.columns and filtered["category"].notna().any():
        pt = pd.pivot_table(
            filtered, values="value", index=group_by, columns="category", aggfunc=agg, fill_value=0
        )
        pt.columns = [category_display_name(c) for c in pt.columns]
        meta["color_var"] = "category"
    else:
        pt = pd.pivot_table(
            filtered, values="value", index=group_by, columns="metric_name", aggfunc=agg, fill_value=0
        )
        pt.columns = [metric_display_name(c) for c in pt.columns]
        meta["color_var"] = "metric_name"
    pct_change_df = None
    if show_pct_change and is_time_series and analysis_type == "Trend" and len(pt) > 1:
        pct_change_df = pt.pct_change(axis=0) * 100
        pct_change_df = pct_change_df.round(1).dropna(how="all")
        pct_change_df.index.name = group_by  # Ensure index name for plotting
    return pt.sort_index(), agg, meta, analysis_type, pct_change_df


def render_analysis_table_and_chart(
    pt: pd.DataFrame,
    pct_change_df: Optional[pd.DataFrame],
    group_by: str,
    display_metrics: List[str],
    agg: str,
    metadata: Any,
    analysis_type: str,
    show_pct_change: bool,
) -> None:
    """
    Render table and interactive chart for analysis.

    Args:
        pt (pd.DataFrame): Pivot table.
        pct_change_df (Optional[pd.DataFrame]): % change DF.
        group_by (str): Group by.
        display_metrics (List[str]): Metrics.
        agg (str): Agg function.
        metadata (Any): Plot metadata.
        analysis_type (str): Type.
        show_pct_change (bool): Show % change.
    """
    if pt is None or pt.empty:
        st.info("No data for the selected filters. Try adjusting your selections above.")
        return
    # Table with optional % change
    col1, col2 = st.columns([2, 1])
    with col1:
        df_to_show = pct_change_df if show_pct_change and pct_change_df is not None else pt
        st.markdown("**Data Table**")
        # Format: % for changes, .2f otherwise
        if show_pct_change and pct_change_df is not None:
            fmt_dict = {col: "{:.1f}%" for col in df_to_show.columns}
        else:
            fmt_dict = {col: "{:.2f}" for col in df_to_show.columns}
        styled = df_to_show.style.format(fmt_dict)
        st.dataframe(styled, use_container_width=True)
        if show_pct_change and pct_change_df is not None:
            st.caption("*% Change is period-over-period (e.g., Q2 vs Q1). First period shows N/A.*")
    with col2:
        st.markdown("**Quick Actions**")
        csv_download(
            df_to_show,
            f"analysis_{analysis_type.lower()}_{agg}{'_pct_change' if show_pct_change else ''}.csv",
        )
        if st.button("🔄 Reset All Filters"):
            st.rerun()

    # Chart rendering
    chart_options = {
        "Auto (Recommended)": "auto",
        "Line Chart (Trends)": "line",
        "Bar Chart (Comparisons)": "bar",
        "Grouped Bar": "group",
        "Stacked Bar": "stack",
        "Pie Chart (Proportions)": "pie",
        "Scatter Plot": "scatter",
        "Scatter with Trendline & R²": "scatter_reg",
        "Heatmap (Correlations)": "heatmap",
    }
    chart_type = st.selectbox(
        "Chart Type",
        list(chart_options.keys()),
        index=0,
        help="Auto picks based on analysis: Line for trends, Bar for comparisons, Scatter for correlations, Pie for proportions.",
    )
    selected_chart = chart_options[chart_type]
    if analysis_type == "Correlation" and metadata:
        df_plot = pt.reset_index()
        x_col = metadata.get("x_col")
        y_col = metadata.get("y_col")
        color_var = metadata.get("color_var")
    else:
        if show_pct_change and pct_change_df is not None:
            melt_df = (
                pct_change_df.reset_index()
                .melt(id_vars=[group_by], var_name=metadata.get("color_var", "variable"), value_name="value")
            )
        else:
            melt_df = (
                pt.reset_index()
                .melt(id_vars=[group_by], var_name=metadata.get("color_var", "variable"), value_name="value")
            )
        df_plot = melt_df
        x_col, y_col = group_by, "value"
        color_var = metadata.get("color_var", "variable")
    if selected_chart == "auto":
        if analysis_type == "Trend" and group_by in ["year", "quarter"]:
            selected_chart = "line"
        elif analysis_type == "Correlation":
            selected_chart = "scatter_reg"
        elif analysis_type == "Comparison":
            selected_chart = "group"
        elif analysis_type == "Proportions":
            selected_chart = "pie"
        else:
            selected_chart = "bar"
    colors = [NDA_GREEN, NDA_ACCENT, PALETTE["info"], PALETTE["violet"], PALETTE["warn"], PALETTE["ok"]]
    fig = go.Figure()
    y_label = "% Change" if show_pct_change and pct_change_df is not None else "Value"
    title = f"{analysis_type} Analysis: {y_label} by {group_by.title()} ({agg.upper()})"
    if show_pct_change:
        title += " — Period-over-Period % Change"
    annotations = []
    if selected_chart == "line":
        fig = px.line(
            df_plot, x=x_col, y=y_col, color=color_var, markers=True, color_discrete_sequence=colors
        )
        fig.update_traces(
            texttemplate="%{y:.1f}" + ("%" if show_pct_change else ""), textposition="top center"
        )
    elif selected_chart in ["bar", "group", "stack"]:
        barmode = "stack" if selected_chart == "stack" else "group"
        fig = px.bar(
            df_plot,
            x=x_col,
            y=y_col,
            color=color_var,
            barmode=barmode,
            color_discrete_sequence=colors,
        )
        fig.update_traces(
            texttemplate="%{y:.1f}" + ("%" if show_pct_change else ""), textposition="outside"
        )
        title += f" ({'Stacked' if barmode == 'stack' else 'Grouped'})"
    elif selected_chart == "pie":
        if len(pt.index) > 1:
            st.warning("Pie charts work best for single periods (proportions). Using Bar chart instead.")
            fig = px.bar(
                df_plot,
                x=x_col,
                y=y_col,
                color=color_var,
                barmode="group",
                color_discrete_sequence=colors,
            )
            fig.update_traces(texttemplate="%{y:.1f}", textposition="outside")
        else:
            fig = px.pie(
                df_plot, values=y_col, names=color_var, hole=0.4, color_discrete_sequence=colors
            )
            fig.update_traces(textinfo="label+percent+value", textposition="inside")
            title = f"Proportions by {color_var.title()} ({agg.upper()})"
    elif selected_chart in ["scatter", "scatter_reg"]:
        trendline = "ols" if selected_chart == "scatter_reg" else None
        fig = px.scatter(
            df_plot,
            x=x_col,
            y=y_col,
            color=color_var if color_var and color_var in df_plot.columns else None,
            trendline=trendline,
            color_discrete_sequence=colors,
        )
        fig.update_traces(texttemplate="%{y:.2f}", mode="markers+text", textposition="top center")
        if trendline and len(df_plot) > 1:
            try:
                slope, intercept, r_value, p_value, std_err = stats.linregress(
                    df_plot[x_col].fillna(0), df_plot[y_col].fillna(0)
                )
                r2 = r_value**2
                annotations.append(
                    dict(
                        text=f"R² = {r2:.3f}<br>p = {p_value:.3g}",
                        xref="paper",
                        yref="paper",
                        x=0.02,
                        y=0.98,
                        showarrow=False,
                        bgcolor="white",
                        bordercolor="grey",
                        borderwidth=1,
                    )
                )
                st.caption(
                    f"**Correlation Insights:** Slope = {slope:.3f}, R² = {r2:.3f} ({'Strong' if abs(r_value) > 0.7 else 'Moderate' if abs(r_value) > 0.3 else 'Weak' if abs(r_value) > 0.1 else 'None'} positive/negative correlation), p-value = {p_value:.3g} ({'significant' if p_value < 0.05 else 'not significant'})"
                )
            except Exception as e:
                st.caption(f"Could not compute regression: {e}")
    elif selected_chart == "heatmap" and pt.shape[1] >= 2:
        if analysis_type != "Correlation":
            corr_matrix = pt.corr(numeric_only=True)
            fig = px.imshow(
                corr_matrix, aspect="auto", color_continuous_scale="RdBu_r", text_auto=True
            )
            title = f"Correlation Heatmap: {y_label} Across {color_var.title()}"
            for i in range(len(corr_matrix.index)):
                for j in range(len(corr_matrix.columns)):
                    val = corr_matrix.iloc[i, j]
                    color = (
                        "green"
                        if val > 0.5
                        else "orange"
                        if val > 0.3
                        else "red"
                        if val < -0.3
                        else "grey"
                    )
                    annotations.append(
                        dict(
                            x=j,
                            y=i,
                            xref="x",
                            yref="y",
                            text=f"{val:.2f}",
                            showarrow=False,
                            font=dict(color=color, size=12),
                        )
                    )
        else:
            selected_chart = "scatter_reg"  # Fallback
            # Re-run scatter logic (omitted for brevity; integrate as needed)

    # Layout updates
    fig.update_layout(
        height=450,
        title=dict(text=title, x=0.5, font=dict(size=14, color=TEXT_DARK)),
        xaxis_title=f"{x_col.replace('_', ' ').title()}",
        yaxis_title=f"{y_label} ({'Count' if agg == 'sum' else agg.title()})",
        plot_bgcolor=CARD_BG,
        paper_bgcolor=CARD_BG,
        font=dict(color=TEXT_DARK),
        legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5),
        annotations=annotations,
    )
    st.plotly_chart(fig, use_container_width=True)


# =======================
# REPORTS DATA FLATTENERS
# =======================
@st.cache_data(show_spinner=False)
def flatten_volumes(data: Dict[str, Any]) -> pd.DataFrame:
    """
    Flatten quarterly and inspection volumes into analysis-ready DF.

    Args:
        data (Dict): Loaded data.

    Returns:
        pd.DataFrame: Flattened volumes.
    """
    rows = []
    for proc in ["MA", "CT"]:
        for qd in data.get("quarterlyVolumes", {}).get(proc, []):
            quarter = qd["quarter"]
            year = int(quarter.split()[-1])
            for metric, value in qd.items():
                if metric == "quarter":
                    continue
                cat = metric.split("_")[-1] if "_" in metric else None
                rows.append(
                    {
                        "source": "volumes",
                        "process": proc,
                        "quarter": quarter,
                        "year": year,
                        "metric_name": metric,
                        "category": cat,
                        "value": (value if isinstance(value, (int, float)) else 0),
                    }
                )
    for qd in data.get("inspectionVolumes", {}).get("GMP", []):
        quarter = qd["quarter"]
        year = int(quarter.split()[-1])
        for metric, value in qd.items():
            if metric == "quarter":
                continue
            cat = metric.split("_")[-1] if "_" in metric else None
            rows.append(
                {
                    "source": "volumes",
                    "process": "GMP",
                    "quarter": quarter,
                    "year": year,
                    "metric_name": metric,
                    "category": cat,
                    "value": (value if isinstance(value, (int, float)) else 0),
                }
            )
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def flatten_steps_for_analytics(data: Dict[str, Any]) -> pd.DataFrame:
    """
    Flatten process steps and bottlenecks for analytics.

    Args:
        data (Dict): Loaded data.

    Returns:
        pd.DataFrame: Flattened steps data.
    """
    rows = []
    # Process steps avgDays/targetDays
    for proc, steps in data.get("processStepData", {}).items():
        for step_key, obj in steps.items():
            for rec in obj.get("data", []):
                quarter = rec.get("quarter")
                if not quarter:
                    continue
                year = int(quarter.split()[-1])
                if "avgDays" in rec:
                    rows.append(
                        {
                            "source": "steps",
                            "process": proc,
                            "quarter": quarter,
                            "year": year,
                            "metric_name": "step_avg_days",
                            "category": strip_disag_suffix(step_key),
                            "value": rec["avgDays"],
                        }
                    )
                if "targetDays" in rec:
                    rows.append(
                        {
                            "source": "steps",
                            "process": proc,
                            "quarter": quarter,
                            "year": year,
                            "metric_name": "step_target_days",
                            "category": strip_disag_suffix(step_key),
                            "value": rec["targetDays"],
                        }
                    )
    # Bottleneck metrics
    for proc, steps in data.get("bottleneckData", {}).items():
        for step, series in steps.items():
            for rec in series:
                quarter = rec.get("quarter")
                if not quarter:
                    continue
                year = int(quarter.split()[-1])
                for m in [
                    "cycle_time_median",
                    "ext_median_days",
                    "opening_backlog",
                    "carry_over_rate",
                    "avg_query_cycles",
                    "fpy_pct",
                    "wait_share_pct",
                    "work_to_staff_ratio",
                    "sched_median_days",
                ]:
                    if rec.get(m) is not None:
                        rows.append(
                            {
                                "source": "bottlenecks",
                                "process": proc,
                                "quarter": quarter,
                                "year": year,
                                "metric_name": m,
                                "category": step,
                                "value": rec[m],
                            }
                        )
    return pd.DataFrame(rows)


def metric_display_name(metric: str) -> str:
    """
    Human-readable name for metrics.

    Args:
        metric (str): Metric key.

    Returns:
        str: Display name.
    """
    m = {
        # Volumes
        "applications_received": "Applications Received",
        "applications_completed": "Applications Completed",
        "approvals_granted": "Approvals Granted",
        "gcp_inspections_requested": "GCP Inspections Requested",
        "gcp_inspections_conducted": "GCP Inspections Conducted",
        "requested_domestic": "Requested - Domestic",
        "requested_foreign": "Requested - Foreign",
        "requested_reliance": "Requested - Reliance",
        "requested_desk": "Requested - Desk/Remote",
        "conducted_domestic": "Conducted - Domestic",
        "conducted_foreign": "Conducted - Foreign",
        "conducted_reliance": "Conducted - Reliance",
        "conducted_desk": "Conducted - Desk/Remote",
        "compliant_domestic": "Compliant - Domestic",
        "compliant_foreign": "Compliant - Foreign",
        "compliant_reliance": "Compliant - Reliance",
        "compliant_desk": "Compliant - Desk/Remote",
        "reports_published": "Reports Published",
        "fir_queries": "FIR Queries",
        "fir_responses": "FIR Responses",
        "queries": "Queries",
        "query_responses": "Query Responses",
        "amendments_received": "Amendments Received",
        "sites_assessed": "Sites Assessed",
        "registry_submissions": "Registry Submissions",
        # Steps/Bottlenecks
        "step_avg_days": "Step Actual Days",
        "step_target_days": "Step Target Days",
        "opening_backlog": "Opening Backlog",
        "cycle_time_median": "Median Cycle Time (Days)",
        "ext_median_days": "Median External Response (Days)",
        "carry_over_rate": "Carry-Over Rate (%)",
        "avg_query_cycles": "Average Query Cycles",
        "fpy_pct": "First Pass Yield (%)",
        "wait_share_pct": "Wait Time Share (%)",
        "work_to_staff_ratio": "Work-to-Staff Ratio",
        "sched_median_days": "Median Scheduling (Days)",
    }
    return m.get(metric, metric.replace("_", " ").title())


def category_display_name(cat: Optional[str]) -> str:
    """
    Human-readable category name.

    Args:
        cat (Optional[str]): Category.

    Returns:
        str: Display name.
    """
    return cat if cat is None else str(cat)


# =======================
# PERIOD FILTER UTILITIES
# =======================
def quarter_order_key(q: str) -> Tuple[int, int]:
    """Sorting key for quarters (Qx YYYY)."""
    qn, yr = q.split()
    return (int(yr), int(qn[1:]))


def filter_period(
    df: pd.DataFrame,
    mode: str,
    q_all: List[str],
    q_single: Optional[str],
    q_from: Optional[str],
    q_to: Optional[str],
    y_from: Optional[int],
    y_to: Optional[int],
) -> pd.DataFrame:
    """
    Filter DF by period mode.

    Args:
        df (pd.DataFrame): Input DF.
        mode (str): Mode ("Single Quarter", etc.).
        q_all (List[str]): All quarters.
        q_single (Optional[str]): Single quarter.
        q_from (Optional[str]): From quarter.
        q_to (Optional[str]): To quarter.
        y_from (Optional[int]): From year.
        y_to (Optional[int]): To year.

    Returns:
        pd.DataFrame: Filtered DF.
    """
    if df.empty:
        return df
    if mode == "Single Quarter" and q_single:
        return df[df["quarter"] == q_single]
    if mode == "Quarter Range" and q_from and q_to:
        q_sorted = sorted(q_all, key=quarter_order_key)
        start_idx, end_idx = q_sorted.index(q_from), q_sorted.index(q_to)
        keep = set(q_sorted[start_idx : end_idx + 1])
        return df[df["quarter"].isin(keep)]
    if mode == "Year Range" and y_from and y_to:
        return df[(df["year"] >= y_from) & (df["year"] <= y_to)]
    return df


# =======================
# OVERVIEW TAB
# =======================
if tab == "Overview":
    process_default = qp_get("process", None)
    quarter_default = qp_get("quarter", None)
    # A signed-in stakeholder is only offered the processes they are entitled to.
    # The payload is already redacted server-side; this stops the UI advertising
    # views that would come back empty.
    allowed_processes = sorted((entitlement or {}).get("processes", [])) or ["MA", "CT", "GMP"]
    allowed_processes = [p for p in ["MA", "CT", "GMP"] if p in allowed_processes]
    process = st.sidebar.radio(
        "Process",
        allowed_processes,
        index=(allowed_processes.index(process_default) if process_default in allowed_processes else 0),
        horizontal=True,
    )
    try:
        st.query_params.update(process=process)
    except Exception:
        st.experimental_set_query_params(process=process)
    quarter = st.sidebar.selectbox(
        "Quarter",
        all_quarters,
        index=(all_quarters.index(quarter_default) if quarter_default in all_quarters else len(all_quarters) - 1),
    )
    try:
        st.query_params.update(quarter=quarter)
    except Exception:
        st.experimental_set_query_params(quarter=quarter)
    disag_choice = st.sidebar.selectbox(
        "Breakdown",
        DISAG_UI_OPTIONS.get(process, ["All"]),
        index=0,
        help="KPIs show general view by default. Choose a disaggregation to view disag-specific trend and steps.",
    )
    process_name = {"MA": "Marketing authorization", "CT": "Clinical trials", "GMP": "Good manufacturing practice"}[process]
    st.markdown(
        f'<div class="context-bar"><strong>{process_name}</strong><span>/</span>'
        f'<span>Performance overview</span><span class="context-period">{escape(quarter)}</span></div>',
        unsafe_allow_html=True,
    )
    kpis_block = data["quarterlyData"][process]
    disagg_variants = {v for mapping in DISAG_KPI_LINKS.values() for v in mapping.values()}
    ordered_ids = [k for k in kpis_block.keys() if k not in disagg_variants]
    default_kpi = qp_get("kpi", ordered_ids[0] if ordered_ids else None)
    if default_kpi not in ordered_ids:
        default_kpi = ordered_ids[0] if ordered_ids else None
    if default_kpi:
        init_state(default_kpi)

    # KPI Details View
    if st.session_state.get(FOCUS_KEY):
        kpi_id = st.session_state[FOCUS_KEY]
        if kpi_id not in kpis_block:
            st.session_state[FOCUS_KEY] = default_kpi
            st.rerun()
        effective_kpi_id, applied = resolve_effective_kpi_id(kpi_id, process, disag_choice)
        k = kpis_block.get(effective_kpi_id) or kpis_block[kpi_id]
        cur = next((x for x in k["data"] if x["quarter"] == quarter), None)
        s = status_for(effective_kpi_id, None if not cur else cur["value"], k.get("target"))
        curr_disp = (
            pct(cur["value"])
            if (effective_kpi_id.startswith("pct_") and cur)
            else (f"{cur['value']:.2f}" if cur else "—")
        )
        curr_label = f" — {applied}" if applied else ""
        status_label = {"success": "On Target", "warning": "Near Target", "error": "Below Target"}.get(
            s, "—"
        )
        applied_badge = (
            f"<span class='kpi-chip' style='margin-left:.5rem;border-color:{NDA_DARK_GREEN}; color:{NDA_DARK_GREEN}'>Filter: {applied}</span>"
            if applied
            else ""
        )
        if disag_choice != "All" and applied is None:
            st.warning(
                f"No disaggregated data available for '{disag_choice}' on this KPI. Showing general view."
            )
        st.markdown(
            f"""
            <div class="detail-hero">
              <h2>{escape(KPI_NAME_MAP.get(effective_kpi_id, {}).get('long', kpi_id))}</h2>
              <p><b>Current{curr_label} &middot; {quarter}</b>: {curr_disp} &nbsp; / &nbsp; <b>Target</b>: {pct(k.get('target')) if effective_kpi_id.startswith('pct_') else k.get('target', '—')} &nbsp; / &nbsp; <b>Baseline</b>: {pct(k.get('baseline')) if effective_kpi_id.startswith('pct_') else k.get('baseline', '—')} &nbsp; / &nbsp; <b>{status_label}</b></p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("← Back to overview", type="primary", use_container_width=True):
            st.session_state[FOCUS_KEY] = None
            try:
                st.query_params.pop("kpi")
            except Exception:
                pass
            st.rerun()
        chart_col1, chart_col2 = st.columns([1.2, 1])
        with chart_col1:
            st.markdown("**What's the volume breakdown for this KPI?**")
            render_kpi_comparison(process, kpi_id, quarter, data)
        with chart_col2:
            st.markdown("**How has this KPI trended over time?**")
            kpi_trend(process, kpi_id, kpis_block, quarter, disag_choice)
        with st.expander(f"🧭 Where are bottlenecks in this process?", expanded=(disag_choice != "All")):
            process_steps_block(process, quarter, data["processStepData"], disag_choice)
        st.stop()

    # Executive Summary Row
    stat_counts = {"success": 0, "warning": 0, "error": 0, "unknown": 0}
    for kid in ordered_ids:
        series = kpis_block[kid]["data"]
        cur = next((x for x in series if x["quarter"] == quarter), None)
        s = status_for(kid, None if not cur else cur["value"], kpis_block[kid].get("target"))
        stat_counts[s] += 1
    total_kpis = sum(stat_counts.values())

    def process_step_status_counts(
        process: str, quarter: str, processStepData: Dict[str, Any]
    ) -> Dict[str, int]:
        """
        Count status for process steps.

        Args:
            process (str): Process.
            quarter (str): Quarter.
            processStepData (Dict): Steps data.

        Returns:
            Dict[str, int]: Status counts.
        """
        all_steps = processStepData.get(process, {})
        counts = {"success": 0, "warning": 0, "error": 0}
        general_steps = {
            k: v for k, v in all_steps.items() if not any(k.endswith(s) for s in DISAG_SUFFIXES)
        }
        for step_name, step_obj in general_steps.items():
            series = step_obj["data"]
            cur = next((x for x in series if x["quarter"] == quarter), None)
            if not cur:
                continue
            metric = cur.get("avgDays")
            target = cur.get("targetDays")
            if metric is None or target is None:
                continue
            status = get_step_status(float(metric), float(target))
            counts[status] += 1
        return counts

    step_counts = process_step_status_counts(process, quarter, data["processStepData"])
    total_steps = sum(step_counts.values())

    summary_items = [
        ("Indicators monitored", str(total_kpis), "Across this regulatory process", "#235D88", "#173E65", "▦"),
        ("Meeting target", str(stat_counts["success"]), f"Of {total_kpis} indicators this quarter", "#087959", "#00543E", "↗"),
        ("Need attention", str(stat_counts["warning"] + stat_counts["error"]), "Near or below the agreed target", "#AA6A23", "#88501A", "!"),
        ("Process steps on track", f"{step_counts['success']} / {total_steps}", "Within target completion time", "#386E70", "#234C59", "◷"),
    ]
    for col, (label, value, note, accent, shade, icon) in zip(st.columns(4), summary_items):
        with col:
            st.markdown(f'<div class="summary-card" style="--accent:{accent};--shade:{shade}"><div class="summary-top"><div class="summary-label">{label}</div><span class="summary-icon">{icon}</span></div><div class="summary-value">{value}</div><div class="summary-note">{note}</div></div>', unsafe_allow_html=True)
    panel_open("Quarter at a glance")
    left, right = st.columns(2)
    for col, counts, total, title, subtitle, key in [
        (left, stat_counts, total_kpis, "Indicator performance", "Outcome measures against agreed targets", "indicators"),
        (right, step_counts, total_steps, "Workflow performance", "Completion times across the regulatory process", "workflow"),
    ]:
        with col, st.container(border=True, key=f"chart_panel_{key}"):
            st.markdown(f'<div class="chart-title">{title}</div><div class="chart-subtitle">{subtitle}</div>', unsafe_allow_html=True)
            labels = ["On track", "At risk", "Off track", "No observations"]
            vals = [counts["success"], counts["warning"], counts["error"], counts.get("unknown", 0)]
            fig = go.Figure(go.Pie(
                values=vals, labels=labels, hole=0.76, sort=False,
                marker=dict(colors=["#12956B", "#E4B24D", "#D96957", "#ADBEB6"], line=dict(color="white", width=5)),
                textinfo="none", hovertemplate="%{label}: %{value}<extra></extra>",
            ))
            center = f"{counts['success'] / total:.0%}" if total else "—"
            fig.update_layout(
                margin=dict(l=16, r=16, t=15, b=45), height=265,
                showlegend=True, plot_bgcolor=CARD_BG, paper_bgcolor=CARD_BG,
                legend=dict(orientation="h", x=.5, xanchor="center", y=-.08, font=dict(size=11)),
                annotations=[dict(text=f"<b>{center}</b><br><span style='font-size:11px;color:#7B8F83'>ON TRACK</span>", x=.5, y=.5, showarrow=False, font=dict(size=32, color="#174535"))],
            )
            st.plotly_chart(fig, use_container_width=True, theme=None, config={"displaylogo": False, "displayModeBar": False})
            st.caption(english_summary(counts, "KPIs" if key == "indicators" else "process steps"))
    with st.expander("How to read this dashboard"):
        st.write("Indicators measure outcomes against their targets. Workflow measures compare completion times with target days. Choose a breakdown in the sidebar, then explore an indicator to see its trends and process steps.")
    panel_close()

    # KPI Grid
    panel_open("Explore the indicators")
    st.caption("Select an indicator to explore trends, targets and the underlying volumes.")
    cols_per_row = 3
    for i in range(0, len(ordered_ids), cols_per_row):
        row_cols = st.columns(cols_per_row)
        for j, kpi_id in enumerate(ordered_ids[i : i + cols_per_row]):
            with row_cols[j]:
                if kpi_card(kpi_id, kpis_block[kpi_id], quarter, process=process):
                    select_kpi(kpi_id, process, quarter)
                    st.rerun()
    panel_close()


# =======================
# REPORTS TAB
# =======================
elif tab == "Reports":
    _reports_processes = sorted((entitlement or {}).get("processes", [])) or ["MA", "CT", "GMP"]
    _reports_processes = [p for p in ["MA", "CT", "GMP"] if p in _reports_processes]
    view = st.sidebar.radio(
        "Reports View",
        ["Answer a question", "Bottleneck Analysis", "Build your own analysis"],
        horizontal=False,
        help="Start with a question. The builder is there if you need a cut we have not anticipated.",
    )

    if view == "Answer a question":
        panel_open("Analysis")
        st.markdown("#### What do you need to know?")
        available_questions = [
            q for q in analytics_answers.QUESTIONS
            if q != "How do our processes compare?" or len(_reports_processes) > 1
        ]
        question = st.radio(
            "Question", available_questions, key="rep_question", label_visibility="collapsed",
            format_func=lambda q: q,
        )
        st.caption(analytics_answers.HINTS[question])
        scope_left, scope_right = st.columns([2, 1], gap="large")
        with scope_left:
            picked = st.multiselect(
                "Processes to include", _reports_processes, default=_reports_processes,
                key="rep_scope",
                help="Defaults to every process you have access to.",
            ) or _reports_processes
        with scope_right:
            as_of = st.selectbox("Quarter", list(reversed(all_quarters)), key="rep_quarter")
        st.divider()
        analytics_answers.render(
            question, data, picked, as_of, all_quarters,
            KPI_NAME_MAP, TIME_BASED, csv_download,
        )
        panel_close()

    if view != "Answer a question":
        process_reports = st.sidebar.selectbox("Process (Reports)", _reports_processes)
        quarter_reports = st.sidebar.selectbox("Quarter (Reports)", all_quarters,
                                               index=len(all_quarters) - 1)

    if view == "Build your own analysis":
        panel_open("Custom analysis")
        st.markdown(
            "**Welcome to Self-Service Analytics!** Build custom views of your regulatory data. Start with Period & Scope, then choose an Analysis Type. Use % Change for trends to spot improvements/declines."
        )
        # Flatten data
        df_vol = flatten_volumes(data)
        df_steps = flatten_steps_for_analytics(data)
        # Period Selection
        with st.expander("📅 Over what time frame should we analyze?", expanded=True):
            st.info("Choose a single quarter, range, or year span for your analysis.")
            period_mode = st.radio(
                "Period Mode", ["Single Quarter", "Quarter Range", "Year Range"], horizontal=True, label_visibility="collapsed"
            )
            q_single = q_from = q_to = None
            y_from = y_to = None
            if period_mode == "Single Quarter":
                q_single = st.selectbox("Select Quarter", all_quarters, index=len(all_quarters) - 1)
            elif period_mode == "Quarter Range":
                c1, c2 = st.columns(2)
                with c1:
                    q_from = st.selectbox("From Quarter", all_quarters, index=max(0, len(all_quarters) - 4))
                with c2:
                    q_to = st.selectbox("To Quarter", all_quarters, index=len(all_quarters) - 1)
                if quarter_order_key(q_from) > quarter_order_key(q_to):
                    st.warning("From > To: Auto-swapping.")
                    q_from, q_to = q_to, q_from
            else:  # Year Range
                years = sorted({int(q.split()[1]) for q in all_quarters})
                c1, c2 = st.columns(2)
                with c1:
                    y_from = st.selectbox("From Year", years, index=max(0, len(years) - 2))
                with c2:
                    y_to = st.selectbox("To Year", years, index=len(years) - 1)
                if y_from > y_to:
                    y_from, y_to = y_to, y_from
        # Scope Selection
        with st.expander("🔍 Which processes and metrics matter most?", expanded=True):
            processes_available = sorted(["MA", "CT", "GMP"])
            processes_selected = st.multiselect(
                "Select Processes",
                processes_available,
                default=[process_reports],
                help="Filter to specific regulatory processes. Leave all for cross-process views.",
            )

            include_steps = st.checkbox(
                "Include Workflow Metrics (steps, backlogs, cycle times, etc.)",
                value=True,
                help="Adds process step delays, bottlenecks like carry-over rates, and medians for deeper insights.",
            )
        # Prepare pool
        pool = pd.concat(
            [df_vol, df_steps if include_steps else pd.DataFrame(columns=df_vol.columns)], ignore_index=True
        )
        if "process" not in pool.columns:
            pool = pd.DataFrame(columns=["process", "metric_name", "quarter", "value"])
        pool = pool[pool["process"].isin(processes_selected)] if processes_selected else pool
        pool = filter_period(pool, period_mode, all_quarters, q_single, q_from, q_to, y_from, y_to)
        if pool.empty:
            st.warning("No data matches your scope & period. Try broadening selections.")
        else:
            # Preview metric
            st.metric(
                "How much data matches your filters?",
                len(pool),
                delta=f"{len(pool['metric_name'].unique())} unique metrics",
            )
        # Analytics Builder
        with st.expander(
            "📈 What kind of analysis do you need—trends, comparisons, or correlations?", expanded=True
        ):
            col1, col2, col3 = st.columns(3)
            with col1:
                analysis_type = st.selectbox(
                    "Analysis Type",
                    ["Trend", "Comparison", "Correlation", "Proportions"],
                    index=0,
                    help="Trend: Over time. Comparison: Side-by-side. Correlation: Relationships. Proportions: Shares/Pies.",
                )
            with col2:
                group_by = st.selectbox(
                    "Group By",
                    ["quarter", "year"],
                    index=0 if analysis_type == "Trend" else 1,
                    help="Quarter for detail, Year for overview.",
                )
            with col3:
                agg = st.selectbox(
                    "Aggregate", ["sum", "mean", "median"], index=1, help="Sum for volumes, Mean/Median for averages/rates."
                )
            # Options
            show_pct_change = False
            if analysis_type == "Trend" and group_by in ["quarter", "year"]:
                show_pct_change = st.checkbox(
                    "Show % Change (vs previous period)",
                    value=True,
                    help="Highlights growth/decline—great for spotting trends!",
                )
            compare_by_category = st.checkbox(
                "Breakdown by Category (e.g., Domestic vs Foreign)",
                value=False,
                help="Splits bars/lines by sub-groups like inspection types.",
            )
            # Metrics
            metrics_in_scope = sorted(pool["metric_name"].unique())
            display_metrics_all = [metric_display_name(m) for m in metrics_in_scope]
            name2key = {d: k for d, k in zip(display_metrics_all, metrics_in_scope)}
            x_metric = y_metric = None
            selected_display_metrics = []
            if analysis_type == "Correlation":
                st.info("For correlations, pick exactly 2 metrics to compare (e.g., Applications vs Approvals).")
                two = st.multiselect(
                    "Select Two Metrics", display_metrics_all, max_selections=2, default=display_metrics_all[:2]
                )
                if len(two) == 2:
                    x_metric = name2key[two[0]]
                    y_metric = name2key[two[1]]
                    selected_display_metrics = two
            else:
                default_metrics = display_metrics_all[:3] if len(display_metrics_all) > 3 else display_metrics_all
                selected_display_metrics = st.multiselect(
                    "Select Metrics (or all for overview)",
                    display_metrics_all,
                    default=default_metrics,
                    help="Choose what to analyze. Fewer = clearer charts.",
                )
            selected_metric_keys = [name2key[d] for d in selected_display_metrics] if selected_display_metrics else []

        # Execute Analysis
        if not pool.empty and selected_metric_keys:
            pt, agg_used, meta, name, pct_df = prep_analysis(
                pool,
                analysis_type,
                processes_selected or ["MA", "CT", "GMP"],
                selected_metric_keys,
                group_by,
                agg,
                compare_by_category,
                show_pct_change,
                x_metric,
                y_metric,
            )
            display_mets = (
                selected_display_metrics
                or [metric_display_name(x_metric), metric_display_name(y_metric)]
                if analysis_type == "Correlation"
                else []
            )
            render_analysis_table_and_chart(
                pt, pct_df, group_by, display_mets, agg_used, meta, name, show_pct_change
            )
        else:
            st.info(
                "👆 Select metrics above to generate your analysis. Example: For trends, pick 'Applications Received' and group by quarter."
            )
        panel_close()
    elif view == "Bottleneck Analysis":
        # Bottleneck Analysis

        @st.cache_data(show_spinner=False)
        def reports_prepare_bottleneck_df(
            process: str, quarter: str, bottleneck_data: Dict[str, Any], allow_estimates: bool = True
        ) -> pd.DataFrame:
            """
            Prepare bottleneck DF with fallback random data if missing.

            Args:
                process (str): Process.
                quarter (str): Quarter.
                bottleneck_data (Dict): Bottlenecks data.

            Returns:
                pd.DataFrame: Bottleneck metrics.
            """
            steps_data = bottleneck_data.get(process, {})
            if not allow_estimates:
                observed_rows = [{"step": step, **row} for step, series in steps_data.items() for row in series if row.get("quarter") == quarter]
                return pd.DataFrame(observed_rows)
            if not steps_data:
                default_steps = {
                    "MA": [
                        "Preliminary Screening",
                        "Technical Dossier Review",
                        "Quality Review",
                        "Safety & Efficacy Review",
                        "Queries to Applicant",
                        "Applicant Response Review",
                        "Decision Issued",
                        "License Publication",
                    ],
                    "CT": [
                        "Administrative Screening",
                        "Ethics Review",
                        "Technical Review",
                        "GCP Inspection",
                        "Applicant Response Review",
                        "Decision Issued",
                        "Trial Registration",
                    ],
                    "GMP": [
                        "Application Screening",
                        "Inspection Planning",
                        "Inspection Conducted",
                        "Inspection Report Drafted",
                        "CAPA Requested",
                        "CAPA Review",
                        "Final Decision Issued",
                        "Report Publication",
                    ],
                }
                steps_data = {step: [] for step in default_steps.get(process, ["Generic Step 1", "Generic Step 2"])}
            rows = []
            for step, series in steps_data.items():
                qrec = next((x for x in series if x.get("quarter") == quarter), None) or {}
                random.seed(f"{process}_{quarter}_{step}")
                row = {"step": step}
                row["cycle_time_median"] = qrec.get("cycle_time_median") or random.uniform(10, 60)
                row["ext_median_days"] = qrec.get("ext_median_days") or random.uniform(5, 30)
                row["opening_backlog"] = qrec.get("opening_backlog") or random.randint(5, 50)
                row["carry_over_rate"] = (qrec.get("carry_over_rate") or random.uniform(0.1, 0.4)) * 100
                row["avg_query_cycles"] = qrec.get("avg_query_cycles") or random.uniform(1, 4)
                row["fpy_pct"] = qrec.get("fpy_pct") or random.uniform(70, 95)
                row["wait_share_pct"] = qrec.get("wait_share_pct") or random.uniform(20, 60)
                if process == "MA":
                    row["work_to_staff_ratio"] = qrec.get("work_to_staff_ratio") or random.uniform(1.5, 4.0)
                else:
                    row["sched_median_days"] = qrec.get("sched_median_days") or random.uniform(7, 21)
                rows.append(row)
            df = pd.DataFrame(rows).sort_values("step")
            if df["cycle_time_median"].isna().any():
                np.random.seed(42)
                df.loc[df["cycle_time_median"].isna(), "cycle_time_median"] = np.random.uniform(
                    10, 60, size=df["cycle_time_median"].isna().sum()
                )
            return df

        panel_open(f"{process_reports} · Workflow bottlenecks")
        df_b = reports_prepare_bottleneck_df(
            process_reports, quarter_reports, data.get("bottleneckData", {}), allow_estimates=not bool(data.get("_meta", {}).get("source"))
        )
        c1, c2 = st.columns(2)
        with c1:
            if df_b.empty or "opening_backlog" not in df_b.columns:
                st.info("No backlog data available for this selection.")
            else:
                st.markdown("**Which steps carry the heaviest backlogs?**")
                backlog_df = df_b[["step", "opening_backlog"]].dropna()
                fig = px.bar(
                    backlog_df,
                    y="step",
                    x="opening_backlog",
                    orientation="h",
                    title=f"Backlog Carried Forward in Process Step ({quarter_reports}, {process_reports})",
                    labels={"opening_backlog": "Backlog Items", "step": "Process Steps"},
                    color_discrete_sequence=[NDA_GREEN],
                )
                fig.update_layout(
                    height=400, plot_bgcolor=CARD_BG, paper_bgcolor=CARD_BG, font=dict(color=TEXT_DARK)
                )
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            if df_b.empty or "cycle_time_median" not in df_b.columns:
                st.info("No cycle time data.")
            else:
                st.markdown("**How long are steps taking to complete?**")
                cycle_df = df_b[["step", "cycle_time_median"]].dropna()
                fig = px.bar(
                    cycle_df,
                    x="step",
                    y="cycle_time_median",
                    title=f"Median Time to Complete This Step (Days, {quarter_reports}, {process_reports})",
                    labels={"cycle_time_median": "Median Days", "step": "Process Steps"},
                    color_discrete_sequence=[NDA_ACCENT],
                )
                fig.update_layout(
                    height=400,
                    xaxis_tickangle=45,
                    plot_bgcolor=CARD_BG,
                    paper_bgcolor=CARD_BG,
                    font=dict(color=TEXT_DARK),
                )
                st.plotly_chart(fig, use_container_width=True)
        st.divider()
        section_header("What are the key metrics driving bottlenecks?", "📋")
        if df_b.empty:
            st.warning("No bottleneck data for this process and quarter.")
        else:
            core_cols = [
                ("cycle_time_median", "⏱️ Time to Complete Step (days)"),
                ("ext_median_days", "⏳ External Response Time (days)"),
                ("opening_backlog", "📦 Backlog at Start of quarter"),
                ("carry_over_rate", "🔄 Carry-Over Rate (%)"),
                ("avg_query_cycles", "❓ Average Queries Needed before completion"),
                ("fpy_pct", "✅ Completed without queries (%)"),
                ("wait_share_pct", "⏸️ Waiting Time (%)"),
            ]
            spec_col = (
                ("work_to_staff_ratio", "Work-to-Staff Ratio")
                if process_reports == "MA"
                else ("sched_median_days", "Median Scheduling Time (Days)")
            )
            display_cols = core_cols + [spec_col]
            raw_cols = [c[0] for c in display_cols]
            display_names = [c[1] for c in display_cols]
            df_display = df_b.reindex(columns=raw_cols + ["step"]).set_index("step")
            df_display.columns = display_names
            st.dataframe(
                df_display.style.format(
                    {
                        "⏱️ Time to Complete Step": "{:.1f} days",
                        "⏳ External Response Time": "{:.1f} days",
                        "📦 Backlog at Start": "{:.0f} items",
                        "🔄 Carry-Over Rate": "{:.1f}%",
                        "❓ Average Queries Needed": "{:.1f}",
                        "✅ First-Time Success Rate": "{:.1f}%",
                        "⏸️ Waiting Time Share": "{:.1f}%",
                        "👥 Workload per Staff": "{:.1f}",
                        "📅 Scheduling Time": "{:.1f} days",
                    },
                    na_rep="—",
                ),
                use_container_width=True,
            )
            st.caption("*NDA Data Analytics.*")
            csv_download(
                df_display.reset_index(),
                f"bottleneck_metrics_{process_reports}_{quarter_reports}.csv",
            )
        panel_close()


# =======================
# DATA DOWNLOADS TAB
# =======================
# Only reachable when OPA says the signed-in stakeholder may export. The tab
# never decides access itself: it renders the catalogue the API offers, and the
# API re-checks every request, so a tampered UI gains nothing.
if tab == "Data downloads":
    rights = st.session_state.get("nda_entitlement") or {}
    token = st.session_state.get("nda_token")
    api_url = os.environ.get("NDA_API_URL", "http://127.0.0.1:8095").rstrip("/")
    headers = {"Authorization": "Bearer " + token} if token else {}

    st.markdown("## Curated data downloads")
    st.caption(
        "Tables you are entitled to export, filtered to your processes. "
        f"Limit {rights.get('row_limit', 0):,} rows per download."
    )

    @st.cache_data(show_spinner=False, ttl=60)
    def load_catalog(bearer: str) -> Dict[str, Any]:
        response = requests.get(api_url + "/v1/catalog", timeout=60,
                                headers={"Authorization": "Bearer " + bearer})
        response.raise_for_status()
        return response.json()

    try:
        catalog = load_catalog(token)
    except requests.RequestException:
        st.error("Could not load the data catalogue. Check the serving API.")
        st.stop()

    datasets = catalog.get("datasets", [])
    if not datasets:
        st.info("Your role does not include data export.")
        st.stop()

    # Choose by what the records ARE, not by where they are stored. The physical
    # layer/table names stay behind the scenes as an id.
    records = sorted({d["record"] for d in datasets})
    record = st.selectbox("What records do you need?", records, key="dl_record")
    for_record = [d for d in datasets if d["record"] == record]
    st.caption(for_record[0]["record_description"])

    processes = sorted({d["process"] for d in for_record})
    left, right = st.columns([3, 2], gap="large")
    with left:
        process = st.selectbox("Which regulatory process?", processes, key="dl_process")
        for_process = [d for d in for_record if d["process"] == process]
        details = sorted({(d["detail_order"], d["detail"]) for d in for_process})
        detail = st.radio("How much detail?", [name for _, name in details], key="dl_detail",
                          help="Reporting data matches the dashboard. Change history is for auditing.")
        chosen = next(d for d in for_process if d["detail"] == detail)
        st.caption(chosen["detail_description"])
    with right:
        fmt = st.radio("File type", catalog.get("formats", ["csv"]),
                       format_func=lambda f: {"csv": "CSV (spreadsheet)", "xlsx": "Excel workbook"}.get(f, f),
                       key="dl_format")
        max_rows = int(catalog.get("row_limit", 50000))
        rows = st.number_input("Maximum rows", min_value=100, max_value=max_rows,
                               value=min(50000, max_rows), step=1000, key="dl_rows")

    layer, table = chosen["layer"], chosen["table"]
    import datetime as _dt
    since = st.date_input("Include records received from", value=_dt.date(2025, 1, 1), key="dl_since")
    since_month = since.replace(day=1)
    if layer == "nda_bronze":
        st.caption("The change history covers everything captured and is not filtered by date.")
    st.markdown(f"**You are about to download:** {chosen['title']} — {chosen['detail'].lower()}")

    if st.button("Prepare download", type="primary", key="dl_go"):
        with st.spinner(f"Querying {layer}.{table}…"):
            try:
                response = requests.get(api_url + "/v1/export", headers=headers, timeout=300,
                                        params={"layer": layer, "table": table, "format": fmt,
                                                "since": since_month.isoformat(), "limit": int(rows)})
            except requests.RequestException:
                st.error("The export service is unavailable.")
                response = None
        if response is not None:
            if response.status_code == 200:
                count = response.headers.get("X-Row-Count", "?")
                slug = re.sub(r"[^a-z0-9]+", "-",
                              f"{chosen['process']} {chosen['record']}".lower()).strip("-")
                name = f"nda-{slug}.{fmt}"
                st.success(f"{count} rows ready — {len(response.content):,} bytes")
                st.download_button(f"Download {name}", data=response.content, file_name=name,
                                   mime=response.headers.get("Content-Type", "application/octet-stream"),
                                   key="dl_file")
            elif response.status_code == 403:
                st.error("Your access does not include this table or format.")
            else:
                st.error(f"Export failed ({response.status_code}).")

    with st.expander("What you are entitled to", expanded=False):
        st.write({
            "groups": rights.get("groups", []),
            "processes": sorted(rights.get("processes", [])),
            "layers": rights.get("layers", []),
            "formats": rights.get("formats", []),
            "row limit": rights.get("row_limit", 0),
            "full dashboard": rights.get("full_dashboard", False),
        })
        st.caption("Granted by your role and evaluated centrally, so the same rules apply "
                   "whether you use this page or the data service directly.")
