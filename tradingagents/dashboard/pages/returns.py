"""Metric-v2 returns surface: four scenario books, never an aggregate fund."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from tradingagents.dashboard.charts import make_equity_curves_facet
from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_generation_metrics,
)

HEADLINE_TITLE = "Four $100k horizon books"
PANEL_LABEL = "Equal-weighted scenario panel"
DEPENDENCE_DISCLOSURE = "Dependent scenario portfolios: shared signals and market data mean the books are not independent observations and are not combined fund AUM."
STRESS_TEST_LABEL = "$5k/$10k/$50k concentration stress tests"
SHARPE_LABEL = "Annualized daily net Sharpe"
INFORMATION_RATIO_LABEL = "Annualized matched-benchmark information ratio"
ACCURACY_LABEL = "Directional accuracy (5 XNYS sessions)"
INSUFFICIENT_HISTORY = "Insufficient history (<30 valid sessions)"


def _rows(books: dict[str, dict]) -> list[dict[str, object]]:
    return [
        {
            "Book": name,
            "Net return": book.get("total_return"),
            SHARPE_LABEL: book.get("annualized_daily_net_sharpe")
            if book.get("annualized_daily_net_sharpe") is not None
            else INSUFFICIENT_HISTORY,
            INFORMATION_RATIO_LABEL: book.get("annualized_matched_information_ratio")
            if book.get("annualized_matched_information_ratio") is not None
            else INSUFFICIENT_HISTORY,
            ACCURACY_LABEL: book.get("directional_accuracy_5d"),
            "Valid sessions": book.get("valid_sessions"),
        }
        for name, book in sorted(books.items())
    ]


def render() -> None:
    st.title("Returns")
    st.caption(DEPENDENCE_DISCLOSURE)
    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return
    selected = st.selectbox("Generation", gens, format_func=lambda item: item["gen_id"])
    report = load_generation_metrics(selected["gen_id"], selected["state_dir"])
    st.subheader(HEADLINE_TITLE)
    panel = report.get("scenario_panel")
    st.metric(
        PANEL_LABEL,
        "Unavailable" if panel is None else f"{panel.get('total_return', 0):+.2%}",
    )
    if panel is None:
        st.info(
            str(
                report.get(
                    "scenario_panel_unavailable_reason", "No available metric epoch."
                )
            )
        )
    history = {
        cohort_id: list(series.get("net_equity_history", []))
        for cohort_id, series in dict(report.get("cohort_series", {}) or {}).items()
        if series.get("net_equity_history")
    }
    st.subheader("Persisted net-equity history")
    if history:
        st.plotly_chart(make_equity_curves_facet(history), use_container_width=True)
    else:
        st.info("No governed metric-v2 equity series is available.")
    st.dataframe(
        pd.DataFrame(_rows(dict(report.get("headline_books", {}) or {}))),
        hide_index=True,
        use_container_width=True,
    )
    st.subheader(STRESS_TEST_LABEL)
    st.dataframe(
        pd.DataFrame(_rows(dict(report.get("stress_tests", {}) or {}))),
        hide_index=True,
        use_container_width=True,
    )
