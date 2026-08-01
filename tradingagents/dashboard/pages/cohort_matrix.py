"""Cohort Matrix — 4x4 heatmap of horizon x size performance."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from tradingagents.dashboard.charts import make_cohort_heatmap
from tradingagents.dashboard.data_loaders import (
    cohort_metric_books,
    get_active_generations,
    load_cohort_heatmap,
    load_cohort_metrics,
)

# Metrics that have data now vs ones requiring closed trades
AVAILABLE_METRICS = {
    "fills": "Fills",
    "strategy_decisions": "Strategy Decisions",
    "closed_trades": "Closed Trades",
    "total_return": "Net Total Return",
    "gross_weight": "Gross Weight",
    "cash_weight": "Cash Weight",
}
CLOSED_TRADE_METRICS = {
    "annualized_daily_net_sharpe": "Annualized daily net Sharpe",
    "annualized_matched_information_ratio": "Annualized matched-benchmark information ratio",
    "max_drawdown": "Net Max Drawdown",
}


def render() -> None:
    st.title("Cohort Matrix")
    st.caption(
        "Dependent scenario portfolios: four $100k horizon books are headline "
        "books; $5k/$10k/$50k concentration stress tests are not combined AUM."
    )

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
        selected_label = st.selectbox(
            "Metric", [m for m in metric_labels if m != "---"], key="matrix_metric"
        )
        # Map label back to key
        selected_metric = "fills"
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
            "This metric is unavailable until its governed v2 sample "
            "requirements are met."
        )

    metric_display = all_metrics.get(selected_metric, selected_metric)
    fig = make_cohort_heatmap(heatmap, metric_display)
    st.plotly_chart(fig, use_container_width=True)

    # ---- Detail table ----
    st.subheader("Cohort Details")
    metrics = load_cohort_metrics(gen["gen_id"], gen["state_dir"])

    rows = []
    for name, m in sorted(cohort_metric_books(metrics).items()):
        parts = name.split("_")
        horizon = parts[1] if len(parts) >= 2 else ""
        size = parts[3] if len(parts) >= 4 else ""
        total_return = m.get("total_return")
        sharpe = m.get("annualized_daily_net_sharpe")
        information_ratio = m.get("annualized_matched_information_ratio")
        unavailable = "Insufficient history (<30 valid sessions)"
        rows.append(
            {
                "Horizon": horizon,
                "Size": f"${size.upper()}" if size != "100k" else "$100K",
                "Decisions": m.get("strategy_decisions", 0),
                "Fills": m.get("fills", 0),
                "Closed": m.get("closed_trades", 0),
                "Net Return": (
                    f"{total_return * 100:.2f}%" if total_return is not None else "—"
                ),
                "Book role": "Headline $100k horizon book"
                if size == "100k"
                else "Concentration stress test",
                "Annualized daily net Sharpe": f"{sharpe:.2f}"
                if sharpe is not None
                else unavailable,
                "Annualized matched-benchmark information ratio": f"{information_ratio:.2f}"
                if information_ratio is not None
                else unavailable,
            }
        )

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)
