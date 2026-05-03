"""Strategy Scorecard — per-strategy signal and trade metrics."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tradingagents.dashboard.charts import GRAY, GREEN, RED, _DARK_LAYOUT, make_strategy_bars
from tradingagents.dashboard.data_loaders import get_active_generations, load_signal_stats


def render() -> None:
    st.title("Strategy Scorecard")

    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return

    gen_options = {g["gen_id"]: g for g in gens}
    selected_gen_id = st.selectbox(
        "Generation", list(gen_options.keys()), key="strat_gen"
    )
    gen = gen_options[selected_gen_id]

    stats = load_signal_stats(gen["gen_id"], gen["state_dir"])
    per_strategy = stats.get("per_strategy", {})

    # ---- Summary metrics ----
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Signals", f"{stats.get('total_signals', 0):,}")
    c2.metric("Traded", f"{stats.get('total_traded', 0):,}")
    trade_rate = (
        stats["total_traded"] / stats["total_signals"] * 100
        if stats.get("total_signals")
        else 0
    )
    c3.metric("Trade Rate", f"{trade_rate:.0f}%")

    # ---- Signal volume chart ----
    fig = make_strategy_bars(per_strategy)
    st.plotly_chart(fig, use_container_width=True)

    # ---- Detail table ----
    st.subheader("Strategy Detail")
    rows = []
    for name, d in sorted(per_strategy.items(), key=lambda x: -x[1].get("signals", 0)):
        hr = d.get("hit_rate_5d")
        rows.append({
            "Strategy": name.replace("_", " ").title(),
            "Signals": d.get("signals", 0),
            "Trades": d.get("trades", 0),
            "Hit Rate 5d": f"{hr*100:.1f}%" if hr is not None else "—",
            "Trade Rate": f"{d.get('trade_rate', 0)*100:.0f}%",
        })

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

    # ---- Knowledge gaps ----
    gaps = stats.get("knowledge_gaps", [])
    if gaps:
        st.subheader("Knowledge Gaps")
        st.caption("Strategies with fewest completed observations — candidates for exploration budget.")

        gap_names = [g.get("strategy", "").replace("_", " ").title() for g in gaps[:10]]
        gap_counts = [g.get("with_outcomes", 0) for g in gaps[:10]]

        fig = go.Figure(go.Bar(
            y=gap_names, x=gap_counts,
            orientation="h",
            marker_color=[RED if c == 0 else GREEN if c >= 5 else GRAY for c in gap_counts],
            hovertemplate="%{y}: %{x} outcomes<extra></extra>",
        ))
        fig.update_layout(
            title="Completed Observations by Strategy",
            xaxis_title="Observations with outcome data",
            height=max(250, len(gap_names) * 30),
            **_DARK_LAYOUT,
        )
        st.plotly_chart(fig, use_container_width=True)
