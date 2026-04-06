"""Paper trading dashboard page."""

import streamlit as st
import pandas as pd

from tradingagents.storage.db import Database


def render(db: Database):
    st.title("Paper Trading")

    # Get strategies in paper status
    paper_strategies = db.get_strategies_by_status("paper")

    if not paper_strategies:
        st.info("No strategies in paper trading. Strategies need to pass backtesting criteria first.")
        return

    st.subheader(f"{len(paper_strategies)} Strategies in Paper Trading")

    for strat in paper_strategies:
        with st.expander(f"{strat['name']} ({strat['instrument']})"):
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Fitness", f"{strat.get('fitness_score', 0):.4f}")
            with col2:
                st.metric("Generation", strat["generation"])
            with col3:
                st.metric("Status", strat["status"])

            # Backtest vs Paper comparison
            backtest = db.get_strategy_backtest(strat["id"])
            paper_trades = db.get_strategy_trades(strat["id"], trade_type="paper")

            if backtest and paper_trades:
                completed = [t for t in paper_trades if t.get("exit_date")]
                if completed:
                    winners = [t for t in completed if (t.get("pnl", 0) or 0) > 0]
                    paper_win_rate = len(winners) / len(completed)

                    st.markdown("**Backtest vs Paper:**")
                    comp_col1, comp_col2 = st.columns(2)
                    with comp_col1:
                        st.metric("Backtest Win Rate", f"{backtest['win_rate']:.0%}")
                    with comp_col2:
                        divergence = abs(paper_win_rate - backtest["win_rate"]) * 100
                        st.metric("Paper Win Rate", f"{paper_win_rate:.0%}",
                                  delta=f"{divergence:.1f}pp divergence")

            # Trade log
            if paper_trades:
                st.markdown("**Recent Trades:**")
                trade_rows = []
                for t in paper_trades[:20]:
                    trade_rows.append({
                        "Ticker": t["ticker"],
                        "Entry": t.get("entry_date", ""),
                        "Exit": t.get("exit_date", ""),
                        "P&L": f"{t.get('pnl', 0):.2f}" if t.get("pnl") else "Open",
                        "P&L %": f"{(t.get('pnl_pct', 0) or 0):.1%}",
                    })
                st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)

            # Graduation indicator
            if backtest:
                st.markdown("**Graduation Readiness:**")
                sharpe_ok = backtest["sharpe"] > 1.0
                trades_ok = len([t for t in paper_trades if t.get("exit_date")]) >= 5
                st.markdown(f"- Sharpe > 1.0: {'Yes' if sharpe_ok else 'No'}")
                st.markdown(f"- 5+ completed trades: {'Yes' if trades_ok else 'No'}")
