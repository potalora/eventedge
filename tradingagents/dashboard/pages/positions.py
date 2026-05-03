"""Open Positions — current positions, concentration, and filtering."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from tradingagents.dashboard.charts import make_position_treemap
from tradingagents.dashboard.data_loaders import get_active_generations, load_all_trades


def render() -> None:
    st.title("Open Positions")

    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return

    # ---- Filters ----
    col1, col2 = st.columns(2)
    with col1:
        gen_options = {g["gen_id"]: g for g in gens}
        selected_gen_id = st.selectbox(
            "Generation", list(gen_options.keys()), key="pos_gen"
        )
    gen = gen_options[selected_gen_id]
    trades = load_all_trades(gen["gen_id"], gen["state_dir"])

    with col2:
        cohorts = sorted({t["cohort"] for t in trades})
        selected_cohort = st.selectbox(
            "Cohort", ["All"] + cohorts, key="pos_cohort"
        )

    if selected_cohort != "All":
        trades = [t for t in trades if t.get("cohort") == selected_cohort]

    open_trades = [t for t in trades if t.get("status") == "open"]

    # ---- Summary metrics ----
    total_deployed = sum(t.get("position_value", 0) for t in open_trades)
    unique_tickers = len({t.get("ticker") for t in open_trades})
    strategies_active = len({t.get("strategy") for t in open_trades})

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open Positions", len(open_trades))
    c2.metric("Unique Tickers", unique_tickers)
    c3.metric("Total Deployed", f"${total_deployed:,.0f}")
    c4.metric("Strategies Active", strategies_active)

    if not open_trades:
        st.info("No open positions.")
        return

    # ---- Positions table ----
    st.subheader("Positions")
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for t in open_trades:
        entry_date = t.get("entry_date", "")
        days_held = 0
        if entry_date:
            try:
                days_held = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
            except ValueError:
                pass

        direction = t.get("direction", "long")
        rows.append({
            "Ticker": t.get("ticker", ""),
            "Strategy": t.get("strategy", "").replace("_", " ").title(),
            "Dir": direction,
            "Entry $": round(t.get("entry_price", 0), 2),
            "Date": entry_date,
            "Shares": t.get("shares", 0),
            "Value": round(t.get("position_value", 0), 2),
            "Days": days_held,
            "Cohort": t.get("cohort", ""),
        })

    df = pd.DataFrame(rows)
    df = df.sort_values("Value", ascending=False)

    st.dataframe(df, use_container_width=True, hide_index=True)

    # ---- Treemap ----
    st.subheader("Concentration")
    fig = make_position_treemap(open_trades)
    st.plotly_chart(fig, use_container_width=True)

    # ---- Ticker overlap across cohorts ----
    if selected_cohort == "All":
        st.subheader("Ticker Overlap Across Cohorts")
        st.caption("Tickers appearing in multiple cohorts (expected — signals are shared within a horizon).")

        ticker_cohorts: dict[str, set[str]] = {}
        for t in open_trades:
            ticker_cohorts.setdefault(t["ticker"], set()).add(t["cohort"])

        overlap = [
            {"Ticker": ticker, "Cohorts": len(cohorts), "In": ", ".join(sorted(cohorts))}
            for ticker, cohorts in sorted(ticker_cohorts.items(), key=lambda x: -len(x[1]))
            if len(cohorts) > 1
        ]
        if overlap:
            st.dataframe(pd.DataFrame(overlap), use_container_width=True, hide_index=True)
        else:
            st.info("No ticker overlap detected.")
