"""Positions are deliberately unavailable until metric v2 exposes a governed projection."""

from __future__ import annotations

import streamlit as st

from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_position_pnl,
)


def render() -> None:
    st.title("Open Positions")
    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return
    generation = st.selectbox(
        "Generation", gens, format_func=lambda item: item["gen_id"]
    )
    load_position_pnl(generation["gen_id"], generation["state_dir"])
    st.info(
        "Position-level ticker detail is unavailable until a governed metric-v2 positions projection exists."
    )
