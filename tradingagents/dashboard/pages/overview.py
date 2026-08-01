"""Autoresearch Overview — generation status, regime, capital deployment."""

from __future__ import annotations

import streamlit as st

from tradingagents.dashboard.charts import (
    REGIME_COLORS,
    make_regime_timeline,
)
from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_generation_metrics,
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

    st.info(
        "$5k/$10k/$50k concentration stress tests are dependent scenarios, not combined fund AUM."
    )

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
        f"Regime: {overall.upper()}</b>"
        f'<span style="margin-left:24px; color:#ccc;">'
        f"VIX {vix:.1f} &nbsp;|&nbsp; Credit {credit:.0f}bps &nbsp;|&nbsp; "
        f"Yield Curve {yc_slope:+.2f} &nbsp;|&nbsp; {ts}</span></div>",
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

    metrics = load_generation_metrics(gen_id, state_dir)
    headline = dict(metrics.get("headline_books", {}) or {})
    total_decisions = sum(c.get("strategy_decisions", 0) for c in headline.values())
    total_fills = sum(c.get("fills", 0) for c in headline.values())
    epoch = metrics.get("epoch") or {}

    st.markdown(f"### {gen_id}")
    st.caption(f"`{commit}` — {desc}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Trading Days", len(run_dates))
    c2.metric("Decisions", f"{total_decisions:,}")
    c3.metric("Fills", f"{total_fills:,}")

    c4, c5, c6 = st.columns(3)
    c4.metric("Started", created)
    c5.metric("Tickers", "Unavailable (no v2 positions projection)")
    c6.metric("Headline Books", len(headline))
    st.caption(
        f"Metric epoch: {epoch.get('epoch_id', 'unavailable')} · "
        f"Schema v{metrics.get('metric_schema_version', 2)} · "
        f"missing headline books: {len(metrics.get('missing_headline_books', []))}"
    )
    st.caption(
        "Four $100k horizon books are dependent scenario portfolios; shared "
        "signals and market data mean they are not independent observations."
    )
