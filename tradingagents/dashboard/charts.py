"""Plotly chart functions for the autoresearch dashboard.

All charts use the plotly_dark template with a consistent color palette.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
_PERCENT_METRICS = frozenset(
    {
        "net total return",
        "gross weight",
        "cash weight",
        "net max drawdown",
    }
)


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
            elif metric_name.lower() in _PERCENT_METRICS:
                row_t.append(f"{val * 100:.1f}%")
            elif isinstance(val, float) and abs(val) < 100:
                row_t.append(f"{val:.2f}")
            else:
                row_t.append(f"{val:,.0f}" if val is not None else "N/A")
        z.append(row_z)
        text.append(row_t)

    all_none = all(
        (heatmap_data.get(h) or {}).get(s) is None
        for h in HORIZON_LABELS
        for s in SIZE_KEYS
    )

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=SIZE_LABELS,
            y=HORIZON_LABELS,
            text=text,
            texttemplate="%{text}",
            textfont=dict(size=14),
            colorscale="RdYlGn" if not all_none else "Greys",
            showscale=True,
            hovertemplate="Horizon: %{y}<br>Size: %{x}<br>Value: %{text}<extra></extra>",
        )
    )

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

    fig.add_trace(
        go.Scatter(
            x=dates,
            y=vix,
            mode="lines+markers",
            name="VIX",
            line=dict(color=BLUE, width=2),
            marker=dict(
                size=8,
                color=[REGIME_COLORS.get(r, GRAY) for r in regimes],
            ),
            hovertemplate="Date: %{x}<br>VIX: %{y:.1f}<extra></extra>",
        )
    )

    max_vix = max(vix) if vix else 40
    y_max = max(max_vix * 1.15, 40)

    shapes = [
        dict(
            type="rect",
            x0=dates[0],
            x1=dates[-1],
            y0=0,
            y1=15,
            fillcolor=GREEN,
            opacity=0.08,
            line_width=0,
            layer="below",
        ),
        dict(
            type="rect",
            x0=dates[0],
            x1=dates[-1],
            y0=15,
            y1=25,
            fillcolor=AMBER,
            opacity=0.06,
            line_width=0,
            layer="below",
        ),
        dict(
            type="rect",
            x0=dates[0],
            x1=dates[-1],
            y0=25,
            y1=35,
            fillcolor=AMBER,
            opacity=0.1,
            line_width=0,
            layer="below",
        ),
        dict(
            type="rect",
            x0=dates[0],
            x1=dates[-1],
            y0=35,
            y1=y_max,
            fillcolor=RED,
            opacity=0.1,
            line_width=0,
            layer="below",
        ),
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


def make_equity_curves_facet(
    history: dict[str, list[dict[str, Any]]],
) -> go.Figure:
    """Plot the persisted metric-v2 return series without rebuilding returns."""
    fig = make_subplots(
        rows=2,
        cols=2,
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
        dates = [record["session"] for record in snaps]
        ret = [record["total_return"] for record in snaps]
        showlegend = s not in seen_sizes
        seen_sizes.add(s)
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=ret,
                name=size_label.get(s, s),
                mode="lines",
                line=dict(color=size_color.get(s, GRAY), width=2),
                legendgroup=s,
                showlegend=showlegend,
                hovertemplate=f"{size_label.get(s, s)}<br>%{{x}}<br>%{{y:+.2%}}<extra></extra>",
            ),
            row=row,
            col=col,
        )
        if dates:
            fig.add_hline(
                y=0,
                line_dash="dot",
                line_color="#444",
                row=row,
                col=col,
            )

    fig.update_layout(
        title="Cumulative Return by Cohort",
        height=560,
        **_DARK_LAYOUT,
    )
    fig.update_yaxes(title_text="Return", tickformat=".1%")
    return fig
