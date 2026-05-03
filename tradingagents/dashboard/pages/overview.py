"""Autoresearch Overview — generation status, regime, capital deployment."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from tradingagents.dashboard.charts import (
    REGIME_COLORS,
    make_capital_bars,
    make_regime_timeline,
)
from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_all_trades,
    load_capital_deployment,
    load_cohort_metrics,
    load_regime_history,
)


def render() -> None:
    st.title("Autoresearch Overview")

    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return

    # ---- Regime banner ----
    _render_regime_banner(gens[0])

    st.markdown("---")

    # ---- Generation cards ----
    cols = st.columns(len(gens))
    for col, gen in zip(cols, gens):
        with col:
            _render_gen_card(gen)

    st.markdown("---")

    # ---- Capital deployment ----
    st.subheader("Capital Deployment")
    gen_tabs = st.tabs([g["gen_id"] for g in gens])
    for tab, gen in zip(gen_tabs, gens):
        with tab:
            dep = load_capital_deployment(gen["gen_id"], gen["state_dir"])
            fig = make_capital_bars(dep)
            st.plotly_chart(fig, use_container_width=True)

    # ---- Regime timeline ----
    st.subheader("Market Regime Timeline")
    regime = load_regime_history(gens[0]["gen_id"], gens[0]["state_dir"])
    fig = make_regime_timeline(regime)
    st.plotly_chart(fig, use_container_width=True)


def _render_regime_banner(gen: dict) -> None:
    """Show current regime as a colored banner."""
    regime = load_regime_history(gen["gen_id"], gen["state_dir"])
    if not regime:
        st.info("No regime data yet.")
        return

    latest = regime[-1]
    overall = latest.get("overall_regime", "unknown")
    vix = latest.get("vix_level", 0)
    credit = latest.get("credit_spread_bps", 0)
    yc_slope = latest.get("yield_curve_slope", 0)
    ts = latest.get("timestamp", "")[:10]

    color = REGIME_COLORS.get(overall, "#6b7280")
    st.markdown(
        f'<div style="background-color:{color}22; border-left:4px solid {color}; '
        f'padding:12px 16px; border-radius:4px; margin-bottom:8px;">'
        f'<b style="color:{color}; font-size:1.2em;">'
        f'Regime: {overall.upper()}</b>'
        f'<span style="margin-left:24px; color:#ccc;">'
        f'VIX {vix:.1f} &nbsp;|&nbsp; Credit {credit:.0f}bps &nbsp;|&nbsp; '
        f'Yield Curve {yc_slope:+.2f} &nbsp;|&nbsp; {ts}</span></div>',
        unsafe_allow_html=True,
    )


def _render_gen_card(gen: dict) -> None:
    """Render a generation summary card."""
    gen_id = gen["gen_id"]
    state_dir = gen["state_dir"]
    created = gen.get("created_at", "")[:10]
    commit = gen.get("git_commit", "")[:7]
    desc = gen.get("description", "")

    # Count successful run dates
    run_dates = set()
    for r in gen.get("run_history", []):
        if r.get("success"):
            run_dates.add(r["date"])

    metrics = load_cohort_metrics(gen_id, state_dir)
    cohorts = metrics.get("cohorts", {})
    total_signals = sum(c.get("total_signals", 0) for c in cohorts.values())
    total_trades = sum(c.get("total_trades", 0) for c in cohorts.values())

    # Deduplicate signals: divide by 4 (4 sizes share signals per horizon)
    unique_signals = total_signals // 4 if total_signals > 0 else 0

    trades = load_all_trades(gen_id, state_dir)
    unique_tickers = len({t.get("ticker") for t in trades})

    st.markdown(f"### {gen_id}")
    st.caption(f"`{commit}` — {desc}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Trading Days", len(run_dates))
    c2.metric("Signals", f"{unique_signals:,}")
    c3.metric("Trades", f"{total_trades:,}")

    c4, c5, c6 = st.columns(3)
    c4.metric("Started", created)
    c5.metric("Tickers", unique_tickers)
    c6.metric("Cohorts", len(cohorts))
