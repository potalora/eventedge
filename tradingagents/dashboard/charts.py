"""Plotly chart functions for the autoresearch dashboard.

All charts use the plotly_dark template with a consistent color palette.
"""
from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

# Color palette
BLUE = "#3b82f6"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#fbbf24"
GRAY = "#6b7280"

REGIME_COLORS = {
    "normal": GREEN,
    "stressed": AMBER,
    "crisis": RED,
}

_DARK_LAYOUT = dict(
    template="plotly_dark",
    margin=dict(l=40, r=20, t=40, b=40),
    font=dict(size=12),
)

# Display labels for sizes
SIZE_LABELS = ["$5K", "$10K", "$50K", "$100K"]
SIZE_KEYS = ["5k", "10k", "50k", "100k"]
HORIZON_LABELS = ["30d", "3m", "6m", "1y"]


def make_cohort_heatmap(
    heatmap_data: dict[str, dict[str, float | None]],
    metric_name: str,
) -> go.Figure:
    """4x4 heatmap: horizons (rows) x sizes (columns).

    Args:
        heatmap_data: {horizon: {size: value}} from CohortComparison.heatmap()
        metric_name: Human-readable metric name for title
    """
    z: list[list[float | None]] = []
    text: list[list[str]] = []

    for h in HORIZON_LABELS:
        row_z: list[float | None] = []
        row_t: list[str] = []
        for s in SIZE_KEYS:
            val = (heatmap_data.get(h) or {}).get(s)
            row_z.append(val if val is not None else 0)
            if val is None:
                row_t.append("N/A")
            elif "rate" in metric_name.lower() or "pct" in metric_name.lower():
                row_t.append(f"{val * 100:.1f}%")
            elif isinstance(val, float) and abs(val) < 100:
                row_t.append(f"{val:.2f}")
            else:
                row_t.append(f"{val:,.0f}" if val is not None else "N/A")
        z.append(row_z)
        text.append(row_t)

    # Check if all values are None/0
    all_none = all(
        (heatmap_data.get(h) or {}).get(s) is None
        for h in HORIZON_LABELS
        for s in SIZE_KEYS
    )

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=SIZE_LABELS,
        y=HORIZON_LABELS,
        text=text,
        texttemplate="%{text}",
        textfont=dict(size=14),
        colorscale="RdYlGn" if not all_none else "Greys",
        showscale=True,
        hovertemplate="Horizon: %{y}<br>Size: %{x}<br>Value: %{text}<extra></extra>",
    ))

    fig.update_layout(
        title=f"Cohort Matrix: {metric_name}",
        xaxis_title="Portfolio Size",
        yaxis_title="Horizon",
        height=350,
        **_DARK_LAYOUT,
    )
    return fig


def make_regime_timeline(regime_snapshots: list[dict[str, Any]]) -> go.Figure:
    """VIX line over time with regime zone bands."""
    fig = go.Figure()

    if not regime_snapshots:
        fig.update_layout(title="Regime Timeline (no data)", **_DARK_LAYOUT, height=300)
        return fig

    dates = [snap.get("timestamp", "")[:10] for snap in regime_snapshots]
    vix = [snap.get("vix_level", 0) for snap in regime_snapshots]
    regimes = [snap.get("overall_regime", "normal") for snap in regime_snapshots]

    # VIX line
    fig.add_trace(go.Scatter(
        x=dates, y=vix,
        mode="lines+markers",
        name="VIX",
        line=dict(color=BLUE, width=2),
        marker=dict(
            size=8,
            color=[REGIME_COLORS.get(r, GRAY) for r in regimes],
        ),
        hovertemplate="Date: %{x}<br>VIX: %{y:.1f}<extra></extra>",
    ))

    # Regime threshold bands
    max_vix = max(vix) if vix else 40
    y_max = max(max_vix * 1.15, 40)

    shapes = [
        dict(type="rect", x0=dates[0], x1=dates[-1], y0=0, y1=15,
             fillcolor=GREEN, opacity=0.08, line_width=0, layer="below"),
        dict(type="rect", x0=dates[0], x1=dates[-1], y0=15, y1=25,
             fillcolor=AMBER, opacity=0.06, line_width=0, layer="below"),
        dict(type="rect", x0=dates[0], x1=dates[-1], y0=25, y1=35,
             fillcolor=AMBER, opacity=0.1, line_width=0, layer="below"),
        dict(type="rect", x0=dates[0], x1=dates[-1], y0=35, y1=y_max,
             fillcolor=RED, opacity=0.1, line_width=0, layer="below"),
    ]

    fig.update_layout(
        title="VIX & Market Regime",
        yaxis_title="VIX Level",
        yaxis_range=[0, y_max],
        shapes=shapes,
        height=300,
        showlegend=False,
        **_DARK_LAYOUT,
    )
    return fig


def make_capital_bars(deployment_data: list[dict[str, Any]]) -> go.Figure:
    """Stacked bar chart: deployed vs available capital per size tier."""
    fig = go.Figure()

    if not deployment_data:
        fig.update_layout(title="Capital Deployment (no data)", **_DARK_LAYOUT, height=300)
        return fig

    # Aggregate across horizons per size
    by_size: dict[str, dict[str, float]] = {}
    for row in deployment_data:
        s = row["size"]
        if s not in by_size:
            by_size[s] = {"deployed": 0, "total_capital": row["total_capital"]}
        by_size[s]["deployed"] += row["deployed"]

    sizes = [f"${s.upper()}" if s != "100k" else "$100K" for s in SIZE_KEYS]
    deployed = [by_size.get(s, {}).get("deployed", 0) for s in SIZE_KEYS]
    total = [by_size.get(s, {}).get("total_capital", 0) for s in SIZE_KEYS]
    # Total across 4 horizons
    total_4h = [t * 4 for t in total]
    available = [t - d for t, d in zip(total_4h, deployed)]

    fig.add_trace(go.Bar(
        x=sizes, y=deployed, name="Deployed",
        marker_color=BLUE,
        hovertemplate="%{x}: $%{y:,.0f} deployed<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=sizes, y=available, name="Available",
        marker_color=GRAY,
        hovertemplate="%{x}: $%{y:,.0f} available<extra></extra>",
    ))

    fig.update_layout(
        title="Capital Deployment (all horizons combined)",
        yaxis_title="Capital ($)",
        barmode="stack",
        height=350,
        **_DARK_LAYOUT,
    )
    return fig


def make_strategy_bars(
    per_strategy: dict[str, dict[str, Any]],
) -> go.Figure:
    """Horizontal bar chart: strategies ranked by signal count, colored by hit rate."""
    fig = go.Figure()

    if not per_strategy:
        fig.update_layout(title="Strategy Signals (no data)", **_DARK_LAYOUT, height=400)
        return fig

    # Sort by signal count descending
    sorted_strats = sorted(
        per_strategy.items(),
        key=lambda x: x[1].get("signals", 0),
    )
    names = [s[0].replace("_", " ").title() for s in sorted_strats]
    signals = [s[1].get("signals", 0) for s in sorted_strats]
    hit_rates = [s[1].get("hit_rate_5d") for s in sorted_strats]

    # Color by hit rate (None → gray)
    colors = []
    for hr in hit_rates:
        if hr is None:
            colors.append(GRAY)
        elif hr >= 0.6:
            colors.append(GREEN)
        elif hr >= 0.4:
            colors.append(AMBER)
        else:
            colors.append(RED)

    hover_text = []
    for s in sorted_strats:
        d = s[1]
        hr = d.get("hit_rate_5d")
        hr_str = f"{hr*100:.0f}%" if hr is not None else "N/A"
        hover_text.append(
            f"Signals: {d.get('signals', 0)}<br>"
            f"Trades: {d.get('trades', 0)}<br>"
            f"Hit Rate: {hr_str}<br>"
            f"Trade Rate: {d.get('trade_rate', 0)*100:.0f}%"
        )

    fig.add_trace(go.Bar(
        y=names, x=signals,
        orientation="h",
        marker_color=colors,
        hovertext=hover_text,
        hoverinfo="text",
    ))

    fig.update_layout(
        title="Signal Volume by Strategy",
        xaxis_title="Signals (deduplicated across sizes)",
        height=max(300, len(names) * 35),
        **_DARK_LAYOUT,
    )
    return fig


def make_equity_curves_facet(
    history: dict[str, list[dict[str, Any]]],
) -> go.Figure:
    """4-panel facet (one per horizon): cumulative return % per size cohort over time."""
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=[f"Horizon {h}" for h in HORIZON_LABELS],
        shared_xaxes=False,
        vertical_spacing=0.15,
        horizontal_spacing=0.08,
    )
    panel_pos = {"30d": (1, 1), "3m": (1, 2), "6m": (2, 1), "1y": (2, 2)}
    size_color = {
        "5k": "#fbbf24",
        "10k": "#22c55e",
        "50k": "#3b82f6",
        "100k": "#a855f7",
    }
    size_label = {"5k": "$5K", "10k": "$10K", "50k": "$50K", "100k": "$100K"}

    if not history:
        fig.update_layout(title="Equity Curves (no data)", **_DARK_LAYOUT, height=500)
        return fig

    seen_sizes: set[str] = set()
    for cohort_name, snaps in sorted(history.items()):
        parts = cohort_name.split("_")
        if len(parts) < 4:
            continue
        h, s = parts[1], parts[3]
        row, col = panel_pos.get(h, (1, 1))
        dates = [r["date"] for r in snaps]
        ret = [r["total_return_pct"] for r in snaps]
        showlegend = s not in seen_sizes
        seen_sizes.add(s)
        fig.add_trace(
            go.Scatter(
                x=dates, y=ret, name=size_label.get(s, s),
                mode="lines",
                line=dict(color=size_color.get(s, GRAY), width=2),
                legendgroup=s,
                showlegend=showlegend,
                hovertemplate=f"{size_label.get(s, s)}<br>%{{x}}<br>%{{y:+.2f}}%<extra></extra>",
            ),
            row=row, col=col,
        )
        # Zero line
        if dates:
            fig.add_hline(
                y=0, line_dash="dot", line_color="#444",
                row=row, col=col,
            )

    fig.update_layout(
        title="Cumulative Return by Cohort",
        height=560,
        **_DARK_LAYOUT,
    )
    fig.update_yaxes(title_text="Return %", ticksuffix="%")
    return fig


def make_gen_comparison(
    histories: dict[str, dict[str, list[dict[str, Any]]]],
    aggregate: str = "mean",
) -> go.Figure:
    """Gen-vs-gen overlay: aggregated return % across cohorts per gen.

    aggregate: 'mean' (avg across 16 cohorts) or 'capital_weighted'.
    """
    fig = go.Figure()
    if not histories:
        fig.update_layout(title="Generation Comparison (no data)", **_DARK_LAYOUT, height=350)
        return fig

    palette = ["#3b82f6", "#22c55e", "#a855f7", "#ef4444", "#fbbf24", "#06b6d4"]
    for i, (gen_id, hist) in enumerate(sorted(histories.items())):
        # Build {date: [returns]} across all cohorts
        bucket: dict[str, list[float]] = {}
        weights: dict[str, list[tuple[float, float]]] = {}
        for cohort, snaps in hist.items():
            for r in snaps:
                d = r["date"]
                bucket.setdefault(d, []).append(r["total_return_pct"])
                cap = r.get("total_capital", 1) or 1
                weights.setdefault(d, []).append((r["total_return_pct"], cap))

        if not bucket:
            continue
        dates = sorted(bucket)
        if aggregate == "capital_weighted":
            ys = []
            for d in dates:
                pairs = weights[d]
                total_cap = sum(c for _, c in pairs)
                ys.append(sum(r * c for r, c in pairs) / total_cap if total_cap else 0)
        else:
            ys = [sum(bucket[d]) / len(bucket[d]) for d in dates]

        color = palette[i % len(palette)]
        fig.add_trace(go.Scatter(
            x=dates, y=ys, name=gen_id,
            mode="lines+markers",
            line=dict(color=color, width=2.5),
            marker=dict(size=6),
            hovertemplate=f"{gen_id}<br>%{{x}}<br>%{{y:+.2f}}%<extra></extra>",
        ))

    fig.add_hline(y=0, line_dash="dot", line_color="#666")
    label = "Capital-Weighted" if aggregate == "capital_weighted" else "Equal-Weighted"
    fig.update_layout(
        title=f"Generation Comparison — {label} Return Across Cohorts",
        yaxis_title="Return %",
        yaxis_ticksuffix="%",
        height=380,
        **_DARK_LAYOUT,
    )
    return fig


def make_drawdown_chart(snapshots: list[dict[str, Any]], label: str = "") -> go.Figure:
    """Underwater plot: cumulative return vs running peak."""
    fig = go.Figure()
    if not snapshots:
        fig.update_layout(title=f"Drawdown {label} (no data)", **_DARK_LAYOUT, height=300)
        return fig

    dates = [s["date"] for s in snapshots]
    pv = [s["portfolio_value"] for s in snapshots]
    peak = []
    cur_peak = pv[0]
    for v in pv:
        cur_peak = max(cur_peak, v)
        peak.append(cur_peak)
    dd_pct = [((v - p) / p * 100) if p else 0 for v, p in zip(pv, peak)]

    fig.add_trace(go.Scatter(
        x=dates, y=dd_pct,
        mode="lines",
        fill="tozeroy",
        line=dict(color=RED, width=1.5),
        fillcolor="rgba(239,68,68,0.25)",
        name="Drawdown",
        hovertemplate="%{x}<br>%{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        title=f"Drawdown {label}".strip(),
        yaxis_title="Drawdown %",
        yaxis_ticksuffix="%",
        height=260,
        showlegend=False,
        **_DARK_LAYOUT,
    )
    return fig


def make_strategy_pnl_chart(rows: list[dict[str, Any]]) -> go.Figure:
    """Stacked horizontal bar: P&L per strategy split by realized/unrealized × long/short."""
    fig = go.Figure()
    if not rows:
        fig.update_layout(title="Strategy P&L (no data)", **_DARK_LAYOUT, height=400)
        return fig

    # Sort by total
    rows = sorted(rows, key=lambda r: r["total_pnl"])
    names = [r["strategy"].replace("_", " ").title() for r in rows]

    components = [
        ("realized_long", "Realized Long", GREEN),
        ("unrealized_long", "Unrealized Long", "#86efac"),
        ("realized_short", "Realized Short", BLUE),
        ("unrealized_short", "Unrealized Short", "#93c5fd"),
    ]
    for key, label, color in components:
        vals = [r[key] for r in rows]
        if not any(v != 0 for v in vals):
            continue
        fig.add_trace(go.Bar(
            y=names, x=vals,
            name=label,
            orientation="h",
            marker_color=color,
            hovertemplate=f"{label}: $%{{x:,.0f}}<extra></extra>",
        ))

    fig.update_layout(
        title="Strategy P&L Attribution",
        xaxis_title="P&L ($)",
        barmode="relative",
        height=max(320, len(names) * 32),
        **_DARK_LAYOUT,
    )
    return fig


def make_winners_losers_bars(
    positions: list[dict[str, Any]], top_n: int = 10,
) -> go.Figure:
    """Side-by-side: top winners (green) and losers (red) by P&L."""
    fig = go.Figure()
    if not positions:
        fig.update_layout(title="Winners & Losers (no data)", **_DARK_LAYOUT, height=350)
        return fig

    # Aggregate per (ticker, strategy, direction) across cohorts
    grouped: dict[tuple, dict] = {}
    for p in positions:
        key = (p["ticker"], p["strategy"], p["direction"])
        d = grouped.setdefault(key, {
            "ticker": p["ticker"], "strategy": p["strategy"],
            "direction": p["direction"], "pnl": 0.0,
        })
        d["pnl"] += p["pnl"]

    items = sorted(grouped.values(), key=lambda x: x["pnl"], reverse=True)
    winners = items[:top_n]
    losers = sorted([i for i in items if i["pnl"] < 0], key=lambda x: x["pnl"])[:top_n]

    def _label(d: dict) -> str:
        side = "↓" if d["direction"] == "short" else "↑"
        return f"{d['ticker']} {side} ({d['strategy'][:8]})"

    if winners:
        fig.add_trace(go.Bar(
            y=[_label(w) for w in winners],
            x=[w["pnl"] for w in winners],
            orientation="h",
            marker_color=GREEN,
            name="Winners",
            hovertemplate="%{y}<br>P&L: $%{x:,.0f}<extra></extra>",
        ))
    if losers:
        fig.add_trace(go.Bar(
            y=[_label(l) for l in losers],
            x=[l["pnl"] for l in losers],
            orientation="h",
            marker_color=RED,
            name="Losers",
            hovertemplate="%{y}<br>P&L: $%{x:,.0f}<extra></extra>",
        ))

    fig.update_layout(
        title=f"Top {top_n} Winners & Losers (aggregated across cohorts)",
        xaxis_title="P&L ($)",
        height=max(400, (len(winners) + len(losers)) * 25),
        barmode="overlay",
        **_DARK_LAYOUT,
    )
    return fig


def make_position_treemap(trades: list[dict[str, Any]]) -> go.Figure:
    """Treemap of open positions: strategy → ticker, sized by position value."""
    fig = go.Figure()

    if not trades:
        fig.update_layout(title="Position Concentration (no data)", **_DARK_LAYOUT, height=400)
        return fig

    labels = ["Portfolio"]
    parents = [""]
    values = [0]
    colors = [""]

    # Strategy palette
    strategy_palette = [
        "#3b82f6", "#22c55e", "#ef4444", "#fbbf24", "#8b5cf6",
        "#06b6d4", "#f97316", "#ec4899", "#14b8a6", "#a855f7",
        "#6366f1", "#84cc16",
    ]
    strat_color_map: dict[str, str] = {}

    # Group trades by strategy → ticker
    by_strategy: dict[str, dict[str, float]] = {}
    for t in trades:
        strat = t.get("strategy", "unknown")
        ticker = t.get("ticker", "?")
        val = t.get("position_value", 0)
        by_strategy.setdefault(strat, {})
        by_strategy[strat][ticker] = by_strategy[strat].get(ticker, 0) + val

    for i, (strat, tickers) in enumerate(sorted(by_strategy.items())):
        strat_label = strat.replace("_", " ").title()
        color = strategy_palette[i % len(strategy_palette)]
        strat_color_map[strat] = color

        # Strategy node
        labels.append(strat_label)
        parents.append("Portfolio")
        values.append(0)
        colors.append(color)

        # Ticker nodes
        for ticker, val in sorted(tickers.items(), key=lambda x: -x[1]):
            labels.append(f"{ticker}")
            parents.append(strat_label)
            values.append(round(val, 2))
            colors.append(color)

    fig = go.Figure(go.Treemap(
        labels=labels,
        parents=parents,
        values=values,
        marker=dict(colors=colors),
        textinfo="label+value",
        texttemplate="%{label}<br>$%{value:,.0f}",
        hovertemplate="%{label}<br>$%{value:,.0f}<extra>%{parent}</extra>",
        branchvalues="total",
    ))

    fig.update_layout(
        title="Position Concentration (by strategy & ticker)",
        height=450,
        **_DARK_LAYOUT,
    )
    return fig
