"""Cohort Matrix — 4x4 heatmap of horizon x size performance."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from tradingagents.dashboard.charts import make_cohort_heatmap
from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_capital_deployment,
    load_cohort_heatmap,
    load_cohort_metrics,
)

# Metrics that have data now vs ones requiring closed trades
AVAILABLE_METRICS = {
    "total_trades": "Total Trades",
    "total_signals": "Total Signals",
    "hit_rate_5d": "Hit Rate (5d)",
    "avg_return_5d": "Avg Return (5d)",
    "open_trades": "Open Trades",
}
CLOSED_TRADE_METRICS = {
    "sharpe": "Sharpe Ratio",
    "win_rate": "Win Rate",
    "avg_pnl": "Avg P&L ($)",
    "max_drawdown_estimate": "Max Drawdown ($)",
}


def render() -> None:
    st.title("Cohort Matrix")

    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return

    # ---- Controls ----
    col1, col2 = st.columns(2)
    with col1:
        gen_options = {g["gen_id"]: g for g in gens}
        selected_gen_id = st.selectbox(
            "Generation", list(gen_options.keys()), key="matrix_gen"
        )
    gen = gen_options[selected_gen_id]

    with col2:
        all_metrics = {**AVAILABLE_METRICS, **CLOSED_TRADE_METRICS}
        metric_labels = (
            list(AVAILABLE_METRICS.values())
            + ["---"]
            + [f"{v} (requires closed trades)" for v in CLOSED_TRADE_METRICS.values()]
        )
        metric_keys = (
            list(AVAILABLE_METRICS.keys())
            + ["__sep__"]
            + list(CLOSED_TRADE_METRICS.keys())
        )
        selected_label = st.selectbox(
            "Metric", [m for m in metric_labels if m != "---"], key="matrix_metric"
        )
        # Map label back to key
        selected_metric = "total_trades"
        for k, lbl in all_metrics.items():
            if lbl in selected_label:
                selected_metric = k
                break

    # ---- Heatmap ----
    heatmap = load_cohort_heatmap(gen["gen_id"], gen["state_dir"], selected_metric)

    # Check if all values are None
    all_none = all(
        (heatmap.get(h) or {}).get(s) is None
        for h in ["30d", "3m", "6m", "1y"]
        for s in ["5k", "10k", "50k", "100k"]
    )

    if all_none and selected_metric in CLOSED_TRADE_METRICS:
        st.info(
            "No closed trades yet — Sharpe, win rate, and P&L metrics will "
            "populate after the first 30-day cycle completes (~April 30)."
        )

    metric_display = all_metrics.get(selected_metric, selected_metric)
    fig = make_cohort_heatmap(heatmap, metric_display)
    st.plotly_chart(fig, use_container_width=True)

    # ---- Detail table ----
    st.subheader("Cohort Details")
    metrics = load_cohort_metrics(gen["gen_id"], gen["state_dir"])
    deployment = load_capital_deployment(gen["gen_id"], gen["state_dir"])
    dep_map = {d["cohort"]: d for d in deployment}

    rows = []
    for name, m in sorted(metrics.get("cohorts", {}).items()):
        parts = name.split("_")
        horizon = parts[1] if len(parts) >= 2 else ""
        size = parts[3] if len(parts) >= 4 else ""
        dep = dep_map.get(name, {})

        hr = m.get("hit_rate_5d")
        rows.append({
            "Horizon": horizon,
            "Size": f"${size.upper()}" if size != "100k" else "$100K",
            "Signals": m.get("total_signals", 0),
            "Trades": m.get("total_trades", 0),
            "Open": m.get("open_trades", 0),
            "Hit Rate 5d": f"{hr*100:.1f}%" if hr is not None else "—",
            "Deployed": f"${dep.get('deployed', 0):,.0f}",
            "Deploy %": f"{dep.get('pct', 0):.0f}%",
        })

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)
