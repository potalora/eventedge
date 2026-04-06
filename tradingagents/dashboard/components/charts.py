import plotly.graph_objects as go
import pandas as pd


def make_equity_curve_chart(equity_df: pd.DataFrame,
                            title: str = "Portfolio Value") -> go.Figure:
    fig = go.Figure()

    if equity_df.empty:
        fig.update_layout(title=title)
        return fig

    fig.add_trace(go.Scatter(
        x=equity_df["date"],
        y=equity_df["portfolio_value"],
        mode="lines",
        name="Portfolio",
        line=dict(color="#3b82f6", width=2),
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Value ($)",
        template="plotly_dark",
        height=400,
    )
    return fig


def make_pnl_bar_chart(trade_log: list, title: str = "Trade P&L") -> go.Figure:
    fig = go.Figure()

    if not trade_log:
        fig.update_layout(title=title)
        return fig

    pnls = [t.get("pnl", 0) for t in trade_log if t.get("pnl") is not None]
    colors = ["#22c55e" if p >= 0 else "#ef4444" for p in pnls]
    labels = [t.get("ticker", "") for t in trade_log if t.get("pnl") is not None]

    fig.add_trace(go.Bar(
        x=labels, y=pnls, marker_color=colors,
    ))

    fig.update_layout(
        title=title, yaxis_title="P&L ($)",
        template="plotly_dark", height=300,
    )
    return fig
