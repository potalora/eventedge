import os
import sys

import streamlit as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tradingagents.storage.db import Database

DB_PATH = os.environ.get(
    "TRADINGAGENTS_DB",
    os.path.join(os.path.dirname(__file__), "../../results/tradingagents.db"),
)


@st.cache_resource
def get_db():
    return Database(DB_PATH)


st.set_page_config(page_title="TradingAgents", layout="wide")

page = st.sidebar.radio("Navigation", ["Portfolio", "Analysis", "Backtest", "Trades", "Leaderboard", "Evolution", "Paper Trading"])

if page == "Portfolio":
    from tradingagents.dashboard.pages.portfolio import render
    render(get_db())
elif page == "Analysis":
    from tradingagents.dashboard.pages.analysis import render
    render(get_db())
elif page == "Backtest":
    from tradingagents.dashboard.pages.backtest import render
    render(get_db())
elif page == "Trades":
    from tradingagents.dashboard.pages.trades import render
    render(get_db())
elif page == "Leaderboard":
    from tradingagents.dashboard.pages.leaderboard import render
    render(get_db())
elif page == "Evolution":
    from tradingagents.dashboard.pages.evolution import render
    render(get_db())
elif page == "Paper Trading":
    from tradingagents.dashboard.pages.paper_trading import render
    render(get_db())
