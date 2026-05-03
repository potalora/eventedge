"""EventEdge Autoresearch Dashboard.

Launch: streamlit run tradingagents/dashboard/app.py
"""
import streamlit as st

st.set_page_config(page_title="EventEdge", layout="wide", page_icon=":chart_with_upwards_trend:")

from tradingagents.dashboard.pages import cohort_matrix, overview, positions, returns, strategies

pages = {
    "Autoresearch": [
        st.Page(overview.render, title="Overview", default=True, url_path="overview"),
        st.Page(returns.render, title="Returns", url_path="returns"),
        st.Page(cohort_matrix.render, title="Cohort Matrix", url_path="cohort-matrix"),
        st.Page(strategies.render, title="Strategies", url_path="strategies"),
        st.Page(positions.render, title="Positions", url_path="positions"),
    ],
}

pg = st.navigation(pages)
pg.run()
