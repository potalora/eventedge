"""Governed strategy-attribution availability surface."""

from __future__ import annotations

import streamlit as st

from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_signal_stats,
)


def render() -> None:
    st.title("Strategy Scorecard")
    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return
    options = {item["gen_id"]: item for item in gens}
    selected = st.selectbox("Generation", list(options), key="strat_gen")
    generation = options[selected]
    stats = load_signal_stats(generation["gen_id"], generation["state_dir"])
    decisions = stats.get("total_signals")
    st.metric(
        "Governed decisions across four $100k horizon books",
        "Unavailable" if decisions is None else f"{decisions:,}",
    )
    st.info(
        "Per-strategy attribution and directional accuracy are unavailable until "
        "the governed metric-v2 report exposes those projections."
    )
