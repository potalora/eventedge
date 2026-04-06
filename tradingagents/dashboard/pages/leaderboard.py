"""Strategy leaderboard dashboard page."""

import streamlit as st
import pandas as pd

from tradingagents.storage.db import Database


def render(db: Database):
    st.title("Strategy Leaderboard")

    # Status filter
    status_filter = st.selectbox(
        "Filter by status",
        ["All", "proposed", "backtested", "active", "paper", "ready", "live", "failed"],
    )

    if status_filter == "All":
        strategies = db.get_top_strategies(limit=50)
    else:
        strategies = db.get_strategies_by_status(status_filter)

    if not strategies:
        st.info("No strategies found. Run strategies to generate strategies.")
        return

    # Build table data
    rows = []
    for i, s in enumerate(strategies):
        rows.append({
            "Rank": i + 1,
            "Name": s["name"],
            "Instrument": s["instrument"],
            "Fitness": f"{s.get('fitness_score', 0):.4f}",
            "Status": s["status"],
            "Generation": s["generation"],
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Expandable details
    st.subheader("Strategy Details")
    strategy_names = [s["name"] for s in strategies]
    selected = st.selectbox("Select strategy", strategy_names)

    if selected:
        strat = next(s for s in strategies if s["name"] == selected)
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Fitness", f"{strat.get('fitness_score', 0):.4f}")
        with col2:
            st.metric("Status", strat["status"])
        with col3:
            st.metric("Generation", strat["generation"])

        st.markdown(f"**Hypothesis:** {strat.get('hypothesis', 'N/A')}")
        st.markdown(f"**Instrument:** {strat['instrument']}")

        entry_rules = strat.get("entry_rules", [])
        exit_rules = strat.get("exit_rules", [])
        st.markdown(f"**Entry rules:** {', '.join(entry_rules) if entry_rules else 'N/A'}")
        st.markdown(f"**Exit rules:** {', '.join(exit_rules) if exit_rules else 'N/A'}")

        # Backtest results
        backtest = db.get_strategy_backtest(strat["id"])
        if backtest:
            st.subheader("Backtest Results")
            bcol1, bcol2, bcol3, bcol4 = st.columns(4)
            with bcol1:
                st.metric("Sharpe", f"{backtest['sharpe']:.2f}")
            with bcol2:
                st.metric("Win Rate", f"{backtest['win_rate']:.0%}")
            with bcol3:
                st.metric("Trades", backtest["num_trades"])
            with bcol4:
                st.metric("Max DD", f"{backtest['max_drawdown']:.1%}")
