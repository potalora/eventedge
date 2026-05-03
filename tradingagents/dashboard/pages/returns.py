"""Returns — equity curves, drawdowns, gen comparisons, P&L attribution."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from tradingagents.dashboard.charts import (
    make_cohort_heatmap,
    make_drawdown_chart,
    make_equity_curves_facet,
    make_gen_comparison,
    make_strategy_pnl_chart,
    make_winners_losers_bars,
)
from tradingagents.dashboard.data_loaders import (
    get_active_generations,
    load_equity_history,
    load_position_pnl,
    load_strategy_pnl,
)


def render() -> None:
    st.title("Returns")
    st.caption(
        "Mark-to-market equity curves, drawdowns, and P&L attribution. "
        "Open positions are valued against the latest yfinance close; closed trades use the recorded exit price."
    )

    gens = get_active_generations()
    if not gens:
        st.warning("No active generations found.")
        return

    histories = {
        g["gen_id"]: load_equity_history(g["gen_id"], g["state_dir"])
        for g in gens
    }

    # --- Gen comparison (always at top) ---
    st.subheader("Generation Comparison")
    agg = st.radio(
        "Aggregation across 16 cohorts",
        ["capital_weighted", "mean"],
        format_func=lambda x: "Capital-weighted" if x == "capital_weighted" else "Equal-weighted",
        horizontal=True,
        key="ret_agg",
    )
    fig = make_gen_comparison(histories, aggregate=agg)
    st.plotly_chart(fig, use_container_width=True)

    # --- Per-gen detail ---
    gen_options = {g["gen_id"]: g for g in gens}
    selected_gen_id = st.selectbox("Generation detail", list(gen_options.keys()), key="ret_gen")
    gen = gen_options[selected_gen_id]
    history = histories[selected_gen_id]

    # ---- Headline cards ----
    _render_summary_cards(gen, history)

    # ---- Return heatmap ----
    st.markdown("### Cohort Return Matrix")
    heatmap = _build_return_heatmap(history)
    fig_hm = make_cohort_heatmap(heatmap, "Total Return %")
    st.plotly_chart(fig_hm, use_container_width=True)

    # ---- Equity curves faceted ----
    st.markdown("### Equity Curves")
    fig_eq = make_equity_curves_facet(history)
    st.plotly_chart(fig_eq, use_container_width=True)

    # ---- Drawdown ----
    st.markdown("### Drawdown")
    cohort_options = sorted(history.keys())
    if cohort_options:
        # Default to the largest 30d cohort if present
        default = "horizon_30d_size_100k"
        idx = cohort_options.index(default) if default in cohort_options else 0
        chosen = st.selectbox("Cohort", cohort_options, index=idx, key="ret_dd_cohort")
        st.plotly_chart(
            make_drawdown_chart(history[chosen], label=f"({chosen})"),
            use_container_width=True,
        )

    # ---- Strategy P&L attribution ----
    st.markdown("### Strategy P&L Attribution")
    strat_rows = load_strategy_pnl(gen["gen_id"], gen["state_dir"])
    fig_strat = make_strategy_pnl_chart(strat_rows)
    st.plotly_chart(fig_strat, use_container_width=True)

    if strat_rows:
        df = pd.DataFrame([
            {
                "Strategy": r["strategy"].replace("_", " ").title(),
                "Total P&L": f"${r['total_pnl']:,.0f}",
                "Realized Long": f"${r['realized_long']:,.0f}",
                "Unrealized Long": f"${r['unrealized_long']:,.0f}",
                "Realized Short": f"${r['realized_short']:,.0f}",
                "Unrealized Short": f"${r['unrealized_short']:,.0f}",
                "Open": r["open_long_count"] + r["open_short_count"],
                "Closed": r["closed_count"],
            }
            for r in strat_rows
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

    # ---- Winners / Losers ----
    st.markdown("### Top Winners & Losers")
    positions = load_position_pnl(gen["gen_id"], gen["state_dir"])
    fig_wl = make_winners_losers_bars(positions, top_n=12)
    st.plotly_chart(fig_wl, use_container_width=True)

    # ---- Detail table ----
    with st.expander("All positions (live mark-to-market)"):
        if positions:
            rows = []
            for p in sorted(positions, key=lambda x: x["pnl"], reverse=True):
                rows.append({
                    "Ticker": p["ticker"],
                    "Strategy": p["strategy"].replace("_", " ").title(),
                    "Dir": p["direction"],
                    "Status": p["status"],
                    "Cohort": p["cohort"],
                    "Entry $": f"{p['entry_price']:.2f}",
                    "Current $": f"{p['current_price']:.2f}",
                    "Shares": int(p["shares"]),
                    "P&L $": f"{p['pnl']:,.2f}",
                    "P&L %": f"{p['pnl_pct']:+.2f}%",
                    "Days": p["days_held"],
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_summary_cards(gen: dict, history: dict) -> None:
    """Headline metrics for a single gen."""
    if not history:
        st.info("No equity history yet for this generation.")
        return

    # Aggregate latest snapshot per cohort
    latest_per_cohort = []
    for snaps in history.values():
        if snaps:
            latest_per_cohort.append(snaps[-1])
    if not latest_per_cohort:
        return

    total_capital = sum(r.get("total_capital", 0) for r in latest_per_cohort)
    total_pv = sum(r.get("portfolio_value", 0) for r in latest_per_cohort)
    realized = sum(r.get("realized_pnl", 0) for r in latest_per_cohort)
    unrealized = sum(r.get("unrealized_pnl", 0) for r in latest_per_cohort)
    total_ret_pct = (total_pv - total_capital) / total_capital * 100 if total_capital else 0

    # Best / worst cohort
    best = max(latest_per_cohort, key=lambda r: r.get("total_return_pct", 0))
    worst = min(latest_per_cohort, key=lambda r: r.get("total_return_pct", 0))

    # Aggregate drawdown across all 16 cohorts (capital-weighted curve)
    by_date_value: dict[str, tuple[float, float]] = {}
    for snaps in history.values():
        for r in snaps:
            d = r["date"]
            cur = by_date_value.get(d, (0.0, 0.0))
            by_date_value[d] = (cur[0] + r["portfolio_value"], cur[1] + r["total_capital"])

    max_dd = 0.0
    if by_date_value:
        dates = sorted(by_date_value)
        peak = 0.0
        for d in dates:
            pv, _ = by_date_value[d]
            peak = max(peak, pv)
            if peak > 0:
                dd = (pv - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "Total Portfolio Value",
        f"${total_pv:,.0f}",
        delta=f"{total_ret_pct:+.2f}% vs ${total_capital:,.0f}",
    )
    c2.metric("Realized P&L", f"${realized:,.0f}")
    c3.metric("Unrealized P&L", f"${unrealized:,.0f}")
    c4.metric("Max Drawdown", f"{max_dd:.2f}%")
    c5.metric(
        "Best / Worst Cohort",
        f"{best['total_return_pct']:+.2f}%",
        delta=f"worst {worst['total_return_pct']:+.2f}%",
        delta_color="off",
    )


def _build_return_heatmap(history: dict) -> dict[str, dict[str, float | None]]:
    """Build {horizon: {size: latest_total_return_pct}} for the heatmap."""
    out: dict[str, dict[str, float | None]] = {h: {} for h in ("30d", "3m", "6m", "1y")}
    for cohort, snaps in history.items():
        parts = cohort.split("_")
        if len(parts) < 4 or not snaps:
            continue
        h, s = parts[1], parts[3]
        out.setdefault(h, {})[s] = snaps[-1].get("total_return_pct")
    return out
