import streamlit as st
import pandas as pd

from tradingagents.storage.db import Database
from tradingagents.storage.queries import get_portfolio_summary, get_recent_signals
from tradingagents.dashboard.components.formatters import format_currency, format_rating_badge
from tradingagents.dashboard.components.charts import make_equity_curve_chart


def render(db: Database):
    st.title("Portfolio Overview")

    summary = get_portfolio_summary(db)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Trades", summary["total_trades"])
    with col2:
        st.metric("Total P&L", format_currency(summary["total_pnl"]))
    with col3:
        pnl_pct = (summary["total_pnl"] / 5000 * 100) if summary["total_trades"] > 0 else 0
        st.metric("Return", f"{pnl_pct:.1f}%")

    st.subheader("Recent Signals")
    signals = get_recent_signals(db, limit=10)
    if signals:
        for s in signals:
            st.markdown(
                f"**{s['ticker']}** ({s['trade_date']}) — "
                f"{format_rating_badge(s['rating'])}",
                unsafe_allow_html=True,
            )
    else:
        st.info("No signals yet. Run an analysis to generate signals.")
