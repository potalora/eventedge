import pandas as pd
import streamlit as st

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.formatters import format_currency


def render(db: Database):
    st.title("Trade History")

    trades = db.get_all_trades()

    if not trades:
        st.info("No trades recorded yet.")
        return

    df = pd.DataFrame(trades)

    col1, col2 = st.columns(2)
    with col1:
        tickers = ["All"] + sorted(df["ticker"].unique().tolist())
        selected_ticker = st.selectbox("Filter by Ticker", tickers)
    with col2:
        actions = ["All"] + sorted(df["action"].unique().tolist())
        selected_action = st.selectbox("Filter by Action", actions)

    if selected_ticker != "All":
        df = df[df["ticker"] == selected_ticker]
    if selected_action != "All":
        df = df[df["action"] == selected_action]

    st.dataframe(df, use_container_width=True)

    csv = df.to_csv(index=False)
    st.download_button("Export CSV", csv, "trades.csv", "text/csv")
