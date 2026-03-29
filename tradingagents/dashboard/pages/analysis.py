import streamlit as st

from tradingagents.storage.db import Database


def render(db: Database):
    st.title("Agent Analysis Reports")

    decisions = db.get_latest_decisions(limit=50)
    tickers = sorted(set(d["ticker"] for d in decisions)) if decisions else []

    if not tickers:
        st.info("No analysis results yet. Run `tradingagents scan` to generate reports.")
        return

    selected = st.selectbox("Select Ticker", tickers)

    ticker_decisions = [d for d in decisions if d["ticker"] == selected]
    if not ticker_decisions:
        return

    latest = ticker_decisions[0]
    st.markdown(f"**Date:** {latest['trade_date']} | **Rating:** {latest['rating']}")

    reports = db.get_reports_for_decision(latest["id"])
    if reports:
        tabs = st.tabs([r["report_type"].title() for r in reports])
        for tab, report in zip(tabs, reports):
            with tab:
                st.markdown(report["content"])
    else:
        st.markdown(latest.get("full_decision", "No detailed report available."))
