"""Question-led analytics for the Reports tab.

The previous self-service builder asked the user to assemble a query - period
mode, processes, metrics, analysis type, group-by, aggregation - before showing
anything. That is six decisions in analyst vocabulary before the first number,
and it assumes the user already knows which cut answers their question.

This module inverts that: it starts from the questions a regulator actually
asks, and each one returns an answer in words first, then the chart that
supports it, then the table behind the chart. The generic builder remains for
people who genuinely want to construct their own cut.

Every function is pure apart from its Streamlit rendering, and takes the data
and the dashboard's own label helpers as arguments, so it can be tested without
importing the dashboard.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# The questions, in the order a review meeting usually asks them.
QUESTIONS: List[str] = [
    "Are we meeting our targets?",
    "Is performance improving or declining?",
    "Where is time being lost?",
    "What is our workload and backlog?",
    "How do our processes compare?",
]


def lower_is_better(kpi_id: str, time_based: set) -> bool:
    """Turnaround and duration indicators improve by going down."""
    return kpi_id in time_based or kpi_id.startswith(("avg_", "median_"))


def label_for(kpi_id: str, name_map: Dict[str, Dict[str, str]]) -> str:
    return name_map.get(kpi_id, {}).get("short", kpi_id.replace("_", " ").title())


def quarter_key(quarter: str) -> tuple:
    part, year = quarter.split()
    return int(year), int(part[1:])


def indicator_frame(data: Dict[str, Any], processes: List[str], quarter: str,
                    name_map: Dict[str, Dict[str, str]], time_based: set) -> pd.DataFrame:
    """One row per indicator for a quarter, with its target and whether it was met."""
    rows = []
    for process in processes:
        for kpi_id, spec in data.get("quarterlyData", {}).get(process, {}).items():
            point = next((d for d in spec.get("data", []) if d["quarter"] == quarter), None)
            if point is None or point.get("value") is None:
                continue
            target = spec.get("target")
            down = lower_is_better(kpi_id, time_based)
            value = float(point["value"])
            met = None
            if target is not None:
                met = value <= float(target) if down else value >= float(target)
            rows.append({
                "Process": process, "kpi_id": kpi_id,
                "Indicator": label_for(kpi_id, name_map),
                "Value": round(value, 1),
                "Target": None if target is None else round(float(target), 1),
                "Met target": met,
                # Signed distance from target, in the direction that means "worse".
                "Shortfall": None if target is None else round(
                    (value - float(target)) if down else (float(target) - value), 1),
                "Unit": "days" if down else "%",
            })
    return pd.DataFrame(rows)


def _no_data(message: str = "No indicator data for this selection.") -> None:
    st.info(message)


def answer_targets(data, processes, quarter, all_quarters, name_map, time_based, export) -> None:
    frame = indicator_frame(data, processes, quarter, name_map, time_based)
    if frame.empty:
        return _no_data()
    scored = frame[frame["Met target"].notna()]
    met = int(scored["Met target"].sum())
    total = len(scored)
    missed = scored[~scored["Met target"]].sort_values("Shortfall", ascending=False)

    if total == 0:
        st.markdown(f"### No indicator in {quarter} has an agreed target to measure against.")
    elif missed.empty:
        st.markdown(f"### Every indicator met its target in {quarter}.")
        st.caption(f"All {total} measured indicators are at or better than target.")
    else:
        worst = missed.iloc[0]
        st.markdown(f"### {met} of {total} indicators met target in {quarter}.")
        st.caption(
            f"{len(missed)} need attention. The largest gap is **{worst['Indicator']}** "
            f"({worst['Process']}) at {worst['Value']}{'' if worst['Unit']=='days' else '%'}"
            f"{' days' if worst['Unit']=='days' else ''} against a target of {worst['Target']}"
            f"{' days' if worst['Unit']=='days' else '%'} — a gap of {worst['Shortfall']}."
        )

    chart_data = scored.copy()
    chart_data["Status"] = chart_data["Met target"].map({True: "Met target", False: "Below target"})
    chart_data = chart_data.sort_values("Shortfall", ascending=True)
    figure = px.bar(
        chart_data, x="Shortfall", y="Indicator", color="Status", orientation="h",
        color_discrete_map={"Met target": "#1F9D6B", "Below target": "#C2554A"},
        hover_data=["Process", "Value", "Target"],
        labels={"Shortfall": "Distance from target (negative is better than target)"},
        height=max(320, 26 * len(chart_data)),
    )
    figure.add_vline(x=0, line_width=1, line_color="#888")
    figure.update_layout(margin=dict(l=8, r=8, t=28, b=8), legend_title_text="")
    st.plotly_chart(figure, use_container_width=True)

    st.markdown("**Indicators below target**" if not missed.empty else "**All indicators**")
    table = (missed if not missed.empty else scored)[
        ["Process", "Indicator", "Value", "Target", "Shortfall", "Unit"]]
    st.dataframe(table, hide_index=True, use_container_width=True)
    export(table.set_index("Indicator"), f"targets_{quarter.replace(' ', '_')}.csv")


def answer_trend(data, processes, quarter, all_quarters, name_map, time_based, export) -> None:
    ordered = sorted(all_quarters, key=quarter_key)
    if len(ordered) < 2:
        return _no_data("At least two quarters are needed to judge a trend.")
    window = st.select_slider("Compare over", options=ordered,
                              value=(ordered[max(0, len(ordered) - 4)], ordered[-1]),
                              key="rep_trend_window")
    first, last = window
    if quarter_key(first) > quarter_key(last):
        first, last = last, first
    span = [q for q in ordered if quarter_key(first) <= quarter_key(q) <= quarter_key(last)]

    rows = []
    for process in processes:
        for kpi_id, spec in data.get("quarterlyData", {}).get(process, {}).items():
            points = {d["quarter"]: d["value"] for d in spec.get("data", []) if d.get("value") is not None}
            start, end = points.get(first), points.get(last)
            if start is None or end is None:
                continue
            down = lower_is_better(kpi_id, time_based)
            change = float(end) - float(start)
            improved = change < 0 if down else change > 0
            direction = "Improving" if abs(change) >= 0.5 and improved else (
                "Declining" if abs(change) >= 0.5 else "Little change")
            rows.append({"Process": process, "kpi_id": kpi_id,
                         "Indicator": label_for(kpi_id, name_map),
                         first: round(float(start), 1), last: round(float(end), 1),
                         "Change": round(change, 1), "Direction": direction})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return _no_data("No indicator has data in both of those quarters.")

    counts = frame["Direction"].value_counts()
    improving, declining = int(counts.get("Improving", 0)), int(counts.get("Declining", 0))
    st.markdown(f"### {improving} improving, {declining} declining between {first} and {last}.")
    if declining:
        worst = frame[frame["Direction"] == "Declining"].reindex(
            frame["Change"].abs().sort_values(ascending=False).index).dropna().head(1)
        if not worst.empty:
            row = worst.iloc[0]
            st.caption(f"Biggest decline: **{row['Indicator']}** ({row['Process']}), "
                       f"{row[first]} → {row[last]}.")
    else:
        st.caption("Nothing went backwards over this period.")

    plot = frame.sort_values("Change")
    figure = px.bar(plot, x="Change", y="Indicator", color="Direction", orientation="h",
                    color_discrete_map={"Improving": "#1F9D6B", "Declining": "#C2554A",
                                        "Little change": "#9AA8A0"},
                    hover_data=["Process", first, last],
                    labels={"Change": f"Change from {first} to {last}"},
                    height=max(320, 26 * len(plot)))
    figure.add_vline(x=0, line_width=1, line_color="#888")
    figure.update_layout(margin=dict(l=8, r=8, t=28, b=8), legend_title_text="")
    st.plotly_chart(figure, use_container_width=True)

    focus = st.multiselect("Show the quarter-by-quarter line for", frame["Indicator"].tolist(),
                           default=frame.sort_values("Change")["Indicator"].head(3).tolist(),
                           key="rep_trend_focus")
    if focus:
        series = []
        for process in processes:
            for kpi_id, spec in data.get("quarterlyData", {}).get(process, {}).items():
                name = label_for(kpi_id, name_map)
                if name not in focus:
                    continue
                for point in spec.get("data", []):
                    if point["quarter"] in span and point.get("value") is not None:
                        series.append({"Quarter": point["quarter"], "Indicator": name,
                                       "Value": round(float(point["value"]), 1)})
        if series:
            line_frame = pd.DataFrame(series)
            line_frame["order"] = line_frame["Quarter"].map(quarter_key)
            line_frame = line_frame.sort_values("order")
            line = px.line(line_frame, x="Quarter", y="Value", color="Indicator", markers=True)
            line.update_layout(margin=dict(l=8, r=8, t=28, b=8), legend_title_text="")
            st.plotly_chart(line, use_container_width=True)

    display = frame[["Process", "Indicator", first, last, "Change", "Direction"]]
    st.dataframe(display, hide_index=True, use_container_width=True)
    export(display.set_index("Indicator"), f"trend_{first}_{last}.csv".replace(" ", "_"))


def answer_time_lost(data, processes, quarter, all_quarters, name_map, time_based, export) -> None:
    """Where the calendar time goes: handling versus queueing, by workflow stage."""
    rows = []
    for process in processes:
        for step, points in data.get("bottleneckData", {}).get(process, {}).items():
            point = next((p for p in points if p["quarter"] == quarter), None)
            if not point:
                continue
            touch = float(point.get("touch_median_days") or 0)
            wait = float(point.get("wait_median_days") or 0)
            rows.append({"Process": process, "Stage": step,
                         "Being worked on": round(touch, 1), "Waiting in queue": round(wait, 1),
                         "Total days": round(touch + wait, 1)})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return _no_data("No workflow timing recorded for this quarter.")

    frame = frame.sort_values("Total days", ascending=False)
    total_days = frame["Total days"].sum()
    total_wait = frame["Waiting in queue"].sum()
    share = (total_wait / total_days * 100) if total_days else 0
    worst = frame.iloc[0]
    st.markdown(f"### {share:.0f}% of elapsed time is spent waiting, not being worked on.")
    st.caption(
        f"Across {len(frame)} stages in {quarter}, {total_wait:.0f} of {total_days:.0f} median days are queueing. "
        f"The slowest stage is **{worst['Stage']}** ({worst['Process']}) at {worst['Total days']} days, "
        f"of which {worst['Waiting in queue']} is waiting."
    )

    melted = frame.melt(id_vars=["Process", "Stage"],
                        value_vars=["Being worked on", "Waiting in queue"],
                        var_name="Time type", value_name="Median days")
    figure = px.bar(melted, x="Median days", y="Stage", color="Time type", orientation="h",
                    color_discrete_map={"Being worked on": "#1F9D6B", "Waiting in queue": "#D9A441"},
                    hover_data=["Process"], height=max(320, 30 * len(frame)))
    figure.update_layout(barmode="stack", margin=dict(l=8, r=8, t=28, b=8), legend_title_text="")
    st.plotly_chart(figure, use_container_width=True)
    st.caption("Queue time is usually the cheapest to remove: it is waiting for a person, "
               "not work being done.")
    st.dataframe(frame, hide_index=True, use_container_width=True)
    export(frame.set_index("Stage"), f"time_lost_{quarter.replace(' ', '_')}.csv")


def answer_workload(data, processes, quarter, all_quarters, name_map, time_based, export) -> None:
    rows = []
    for process in processes:
        for entry in data.get("quarterlyVolumes", {}).get(process, []):
            received = entry.get("applications_received")
            completed = entry.get("applications_completed")
            if received is None and completed is None:
                continue
            rows.append({"Process": process, "Quarter": entry["quarter"],
                         "Received": int(received or 0), "Completed": int(completed or 0)})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return _no_data("No volume data is available for your access level.")
    frame["order"] = frame["Quarter"].map(quarter_key)
    frame = frame.sort_values("order")
    totals = frame.groupby("Quarter", sort=False)[["Received", "Completed"]].sum().reset_index()
    totals["Net change in open work"] = totals["Received"] - totals["Completed"]
    totals["Open at end of quarter"] = totals["Net change in open work"].cumsum()

    latest = totals.iloc[-1]
    direction = "grew" if latest["Net change in open work"] > 0 else (
        "shrank" if latest["Net change in open work"] < 0 else "held steady")
    st.markdown(f"### The backlog {direction} in {latest['Quarter']}.")
    st.caption(
        f"{int(latest['Received'])} applications arrived and {int(latest['Completed'])} were completed, "
        f"leaving roughly {int(latest['Open at end of quarter'])} still open."
    )

    figure = go.Figure()
    figure.add_bar(x=totals["Quarter"], y=totals["Received"], name="Received", marker_color="#2E7FA8")
    figure.add_bar(x=totals["Quarter"], y=totals["Completed"], name="Completed", marker_color="#1F9D6B")
    figure.add_trace(go.Scatter(x=totals["Quarter"], y=totals["Open at end of quarter"],
                                name="Open at end of quarter", mode="lines+markers",
                                line=dict(color="#C2554A", width=2)))
    figure.update_layout(barmode="group", margin=dict(l=8, r=8, t=28, b=8),
                         legend_title_text="", height=420)
    st.plotly_chart(figure, use_container_width=True)
    st.dataframe(totals.drop(columns=[]), hide_index=True, use_container_width=True)
    export(totals.set_index("Quarter"), "workload_and_backlog.csv")


def answer_compare(data, processes, quarter, all_quarters, name_map, time_based, export) -> None:
    if len(processes) < 2:
        return _no_data("Comparing processes needs access to more than one of them.")
    frame = indicator_frame(data, processes, quarter, name_map, time_based)
    if frame.empty:
        return _no_data()
    scored = frame[frame["Met target"].notna()]
    summary = scored.groupby("Process").agg(
        Indicators=("Indicator", "count"),
        **{"Met target": ("Met target", "sum")}).reset_index()
    summary["% meeting target"] = (summary["Met target"] / summary["Indicators"] * 100).round(0)

    cycle = []
    for process in processes:
        stages = data.get("bottleneckData", {}).get(process, {})
        days = [float(p.get("cycle_time_median") or 0)
                for step in stages.values() for p in step if p["quarter"] == quarter]
        if days:
            cycle.append({"Process": process, "Median stage time (days)": round(sum(days) / len(days), 1)})
    if cycle:
        summary = summary.merge(pd.DataFrame(cycle), on="Process", how="left")

    best = summary.sort_values("% meeting target", ascending=False).iloc[0]
    worst = summary.sort_values("% meeting target", ascending=True).iloc[0]
    st.markdown(f"### {best['Process']} is meeting the most targets in {quarter}.")
    st.caption(
        f"{best['Process']} met {int(best['Met target'])} of {int(best['Indicators'])} "
        f"({best['% meeting target']:.0f}%); {worst['Process']} met "
        f"{int(worst['Met target'])} of {int(worst['Indicators'])} ({worst['% meeting target']:.0f}%)."
    )
    figure = px.bar(summary, x="Process", y="% meeting target", color="Process",
                    text="% meeting target", height=380)
    figure.update_traces(texttemplate="%{text:.0f}%", textposition="outside")
    figure.update_layout(margin=dict(l=8, r=8, t=28, b=8), showlegend=False,
                         yaxis_range=[0, 110])
    st.plotly_chart(figure, use_container_width=True)
    st.dataframe(summary, hide_index=True, use_container_width=True)
    export(summary.set_index("Process"), f"process_comparison_{quarter.replace(' ', '_')}.csv")


ANSWERS: Dict[str, Callable] = {
    QUESTIONS[0]: answer_targets,
    QUESTIONS[1]: answer_trend,
    QUESTIONS[2]: answer_time_lost,
    QUESTIONS[3]: answer_workload,
    QUESTIONS[4]: answer_compare,
}

HINTS: Dict[str, str] = {
    QUESTIONS[0]: "Every indicator against its agreed target for one quarter, worst gap first.",
    QUESTIONS[1]: "Direction of travel between two quarters, so you can see what is getting better or worse.",
    QUESTIONS[2]: "Splits elapsed time into work and queueing, by workflow stage.",
    QUESTIONS[3]: "What arrived, what was finished, and how the open caseload moved.",
    QUESTIONS[4]: "The three regulatory processes side by side on the same measures.",
}


def render(question: str, data, processes, quarter, all_quarters, name_map, time_based, export) -> None:
    ANSWERS[question](data, processes, quarter, all_quarters, name_map, time_based, export)
