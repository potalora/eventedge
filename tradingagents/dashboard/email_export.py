"""Render the autoresearch dashboard as a self-contained HTML email.

Reuses tradingagents.dashboard.data_loaders for state I/O and
tradingagents.dashboard.charts for figure construction. Charts are exported to
PNG via kaleido and embedded as base64 data URIs so the resulting HTML is
fully self-contained (forwards cleanly through Gmail).
"""
from __future__ import annotations

import base64
import html
import logging
import warnings
from html import escape
from typing import Any, Iterable

# Suppress noisy Streamlit warnings BEFORE importing data_loaders (which imports
# streamlit). The warnings come from @st.cache_data running outside a script run.
def _silence_streamlit() -> None:
    warnings.filterwarnings("ignore", message=".*ScriptRunContext.*")
    warnings.filterwarnings("ignore", message=".*No runtime found.*")
    for name in (
        "streamlit",
        "streamlit.runtime",
        "streamlit.runtime.caching",
        "streamlit.runtime.caching.cache_data_api",
        "streamlit.runtime.scriptrunner_utils",
        "streamlit.runtime.scriptrunner_utils.script_run_context",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(logging.CRITICAL + 1)
        lg.propagate = False
        lg.disabled = True


_silence_streamlit()

import plotly.graph_objects as go

from tradingagents.dashboard import charts as ch
from tradingagents.dashboard import data_loaders as dl

_silence_streamlit()  # Re-apply after streamlit import attaches its own handlers.

logger = logging.getLogger(__name__)

CHART_WIDTH = 760
PLACEHOLDER = '<p class="placeholder">[chart unavailable]</p>'


def _chart_to_png_b64(fig: go.Figure, width: int = CHART_WIDTH, height: int | None = None) -> str:
    """Render a plotly figure to a base64-encoded PNG. Returns "" on failure."""
    try:
        kwargs = {"format": "png", "width": width, "scale": 2}
        if height is not None:
            kwargs["height"] = height
        img_bytes = fig.to_image(**kwargs)
        return base64.b64encode(img_bytes).decode("ascii")
    except Exception as exc:
        logger.warning("chart render failed: %s", exc)
        return ""


def _img_tag(b64: str, alt: str) -> str:
    if not b64:
        return PLACEHOLDER
    return f'<img src="data:image/png;base64,{b64}" alt="{escape(alt)}" />'


def _fmt_money(v: float | int | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}%"


def _fmt_num(v: float | int | None) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{v:,}"


def _regime_badge(regime: str | None) -> str:
    if not regime:
        return '<span class="badge badge-muted">unknown</span>'
    color_class = {
        "normal": "badge-green",
        "stressed": "badge-amber",
        "crisis": "badge-red",
    }.get(regime, "badge-muted")
    return f'<span class="badge {color_class}">{escape(regime)}</span>'


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_header(gen_meta: dict[str, Any], date: str, regime: str | None) -> str:
    gen_id = gen_meta.get("gen_id", "?")
    started = gen_meta.get("created_at", "")[:10] or "?"
    description = gen_meta.get("description", "")
    commit = (gen_meta.get("git_commit") or "")[:7]
    return f"""
    <div class="header">
      <div class="header-top">
        <h1>{escape(gen_id)}</h1>
        {_regime_badge(regime)}
      </div>
      <div class="header-meta">
        <span>Snapshot {escape(date)}</span>
        <span>Started {escape(started)}</span>
        {f'<span>Commit {escape(commit)}</span>' if commit else ''}
      </div>
      {f'<p class="header-desc">{escape(description)}</p>' if description else ''}
    </div>
    """


def _is_nan(v: Any) -> bool:
    import math
    return isinstance(v, float) and (math.isnan(v) or math.isinf(v))


def _safe_num(v: Any) -> float | None:
    """Return v if a finite number, else None."""
    if v is None:
        return None
    if _is_nan(v):
        return None
    return v if isinstance(v, (int, float)) else None


def _latest_equity(equity: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Return {cohort: latest_snapshot} from equity history. Skips snapshots
    whose total_return_pct is NaN by walking back to the last clean one."""
    out: dict[str, dict[str, Any]] = {}
    for cohort, snaps in equity.items():
        for snap in reversed(snaps):
            if _safe_num(snap.get("total_return_pct")) is not None:
                out[cohort] = snap
                break
        else:
            if snaps:
                out[cohort] = snaps[-1]
    return out


def _render_kpis(
    metrics: dict[str, Any],
    capital_rows: list[dict[str, Any]],
    signal_stats: dict[str, Any],
    positions: list[dict[str, Any]],
    equity: dict[str, list[dict[str, Any]]],
) -> str:
    cohorts = metrics.get("cohorts", {}) or {}
    latest = _latest_equity(equity)

    total_deployed = sum(r.get("deployed", 0) for r in capital_rows)
    total_capital = sum(r.get("total_capital", 0) for r in capital_rows)
    deployed_pct = (total_deployed / total_capital * 100) if total_capital else 0

    # Capital-weighted total return across cohorts (from equity snapshots).
    weighted_num = 0.0
    weighted_den = 0.0
    for cohort_name, snap in latest.items():
        ret = _safe_num(snap.get("total_return_pct"))
        cap = _safe_num(snap.get("total_capital")) or 0
        if ret is not None and cap:
            weighted_num += ret * cap
            weighted_den += cap
    weighted_return = (weighted_num / weighted_den) if weighted_den else None

    open_positions = sum(1 for p in positions if p.get("status") == "open")
    total_signals = signal_stats.get("total_signals", 0)
    total_traded = signal_stats.get("total_traded", 0)

    tiles = [
        ("Capital deployed", f"{_fmt_money(total_deployed)}", f"{deployed_pct:.1f}% of {_fmt_money(total_capital)}"),
        ("Weighted return", _fmt_pct(weighted_return), f"across {len(cohorts)} cohorts"),
        ("Open positions", f"{open_positions:,}", f"{sum(1 for p in positions if p.get('status') == 'closed')} closed"),
        ("Signals (cum.)", f"{total_signals:,}", f"{total_traded} traded"),
    ]

    cells = "".join(
        f'<div class="kpi-tile"><div class="kpi-label">{escape(label)}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-sub">{escape(sub)}</div></div>'
        for label, value, sub in tiles
    )
    return f'<div class="kpi-row">{cells}</div>'


def _render_cohort_matrix(
    metrics: dict[str, Any],
    equity: dict[str, list[dict[str, Any]]],
) -> str:
    cohorts = metrics.get("cohorts", {}) or {}
    latest = _latest_equity(equity)

    # Build heatmap data from equity history (cohort metrics doesn't include return %).
    heatmap_data: dict[str, dict[str, float | None]] = {}
    for h in ch.HORIZON_LABELS:
        heatmap_data[h] = {}
        for s in ch.SIZE_KEYS:
            name = f"horizon_{h}_size_{s}"
            snap = latest.get(name) or {}
            heatmap_data[h][s] = snap.get("total_return_pct")

    has_any = any(v is not None for row in heatmap_data.values() for v in row.values())
    if has_any:
        fig = ch.make_cohort_heatmap(heatmap_data, "Total Return %")
        b64 = _chart_to_png_b64(fig, height=380)
    else:
        b64 = ""

    # Trade-count table
    rows = ['<tr><th>Horizon</th>'] + [f'<th>{lbl}</th>' for lbl in ch.SIZE_LABELS] + ['</tr>']
    header = "".join(rows)
    body_rows = []
    for h in ch.HORIZON_LABELS:
        cells = [f'<th class="row-head">{h}</th>']
        for s in ch.SIZE_KEYS:
            name = f"horizon_{h}_size_{s}"
            m = cohorts.get(name) or {}
            snap = latest.get(name) or {}
            n_open = m.get("open_trades", snap.get("n_open", 0)) or 0
            n_closed = m.get("closed_trades", snap.get("n_closed", 0)) or 0
            ret = _safe_num(snap.get("total_return_pct"))
            ret_str = _fmt_pct(ret) if ret is not None else "—"
            ret_class = "pos" if (ret or 0) > 0 else ("neg" if (ret or 0) < 0 else "")
            cells.append(
                f'<td><div class="cell-ret {ret_class}">{ret_str}</div>'
                f'<div class="cell-sub">{n_open}o · {n_closed}c</div></td>'
            )
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    table = (
        '<table class="matrix-table">'
        f"<thead>{header}</thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )

    return f"""
    <section>
      <h2>Cohort matrix</h2>
      {_img_tag(b64, "Cohort return heatmap")}
      {table}
    </section>
    """


def _render_equity_curves(history: dict[str, list[dict[str, Any]]]) -> str:
    if not history:
        return '<section><h2>Equity curves</h2><p class="muted">No equity history yet.</p></section>'
    fig = ch.make_equity_curves_facet(history)
    b64 = _chart_to_png_b64(fig, height=560)
    return f'<section><h2>Equity curves</h2>{_img_tag(b64, "Equity curves by horizon")}</section>'


def _render_strategy_pnl(strategy_rows: list[dict[str, Any]]) -> str:
    if not strategy_rows:
        return '<section><h2>Strategy P&amp;L</h2><p class="muted">No strategy P&amp;L yet.</p></section>'
    fig = ch.make_strategy_pnl_chart(strategy_rows)
    b64 = _chart_to_png_b64(fig, height=max(320, len(strategy_rows) * 32))

    head = (
        '<tr><th>Strategy</th><th class="num">Total</th>'
        '<th class="num">Realized L</th><th class="num">Realized S</th>'
        '<th class="num">Unreal. L</th><th class="num">Unreal. S</th>'
        '<th class="num">Open L/S</th></tr>'
    )
    body = []
    for r in strategy_rows[:15]:
        total = r.get("total_pnl", 0)
        cls = "pos" if total > 0 else ("neg" if total < 0 else "")
        body.append(
            "<tr>"
            f"<td>{escape(r['strategy'].replace('_', ' ').title())}</td>"
            f'<td class="num {cls}">{_fmt_money(total)}</td>'
            f'<td class="num">{_fmt_money(r.get("realized_long", 0))}</td>'
            f'<td class="num">{_fmt_money(r.get("realized_short", 0))}</td>'
            f'<td class="num">{_fmt_money(r.get("unrealized_long", 0))}</td>'
            f'<td class="num">{_fmt_money(r.get("unrealized_short", 0))}</td>'
            f'<td class="num">{r.get("open_long_count", 0)}/{r.get("open_short_count", 0)}</td>'
            "</tr>"
        )
    table = (
        '<table class="data-table">'
        f"<thead>{head}</thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
    )

    return f'<section><h2>Strategy P&amp;L</h2>{_img_tag(b64, "Strategy P&L")}{table}</section>'


def _render_winners_losers(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return '<section><h2>Winners &amp; losers</h2><p class="muted">No positions yet.</p></section>'
    fig = ch.make_winners_losers_bars(positions, top_n=10)
    b64 = _chart_to_png_b64(fig, height=480)
    return f'<section><h2>Winners &amp; losers</h2>{_img_tag(b64, "Winners and losers")}</section>'


def _render_positions_table(positions: list[dict[str, Any]], top_n: int = 50) -> str:
    if not positions:
        return ""
    open_pos = [p for p in positions if p.get("status") == "open"]
    if not open_pos:
        return '<section><h2>Open positions</h2><p class="muted">No open positions.</p></section>'

    open_pos = sorted(open_pos, key=lambda p: abs(p.get("pnl", 0)), reverse=True)[:top_n]

    head = (
        '<tr><th>Ticker</th><th>Strategy</th><th>Cohort</th>'
        '<th class="num">Entry</th><th class="num">Last</th>'
        '<th class="num">Shares</th><th class="num">P&amp;L</th>'
        '<th class="num">P&amp;L %</th><th class="num">Days</th></tr>'
    )
    body = []
    for p in open_pos:
        pnl = p.get("pnl", 0)
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        direction_arrow = "↓" if p.get("direction") == "short" else "↑"
        body.append(
            "<tr>"
            f"<td><strong>{escape(p.get('ticker', '?'))}</strong> {direction_arrow}</td>"
            f"<td>{escape((p.get('strategy') or '').replace('_', ' '))}</td>"
            f"<td>{escape(p.get('cohort', '').replace('horizon_', '').replace('_size_', '/'))}</td>"
            f'<td class="num">${p.get("entry_price", 0):.2f}</td>'
            f'<td class="num">${p.get("current_price", 0):.2f}</td>'
            f'<td class="num">{p.get("shares", 0):.0f}</td>'
            f'<td class="num {cls}">{_fmt_money(pnl)}</td>'
            f'<td class="num {cls}">{_fmt_pct(p.get("pnl_pct", 0))}</td>'
            f'<td class="num">{p.get("days_held", 0)}</td>'
            "</tr>"
        )
    table = (
        '<table class="data-table">'
        f"<thead>{head}</thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
    )

    return f'<section><h2>Open positions (top {len(open_pos)} by |P&amp;L|)</h2>{table}</section>'


def _render_regime_timeline(snapshots: list[dict[str, Any]]) -> str:
    if not snapshots:
        return ""
    fig = ch.make_regime_timeline(snapshots)
    b64 = _chart_to_png_b64(fig, height=300)
    return f'<section><h2>Regime timeline</h2>{_img_tag(b64, "VIX & regime timeline")}</section>'


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

BENCHMARK_TICKERS = ["SPY", "RSP", "IWM", "QQQ", "AGG"]
BENCHMARK_LABELS = {
    "SPY": "S&P 500",
    "RSP": "S&P 500 Equal-Weight",
    "IWM": "Russell 2000 (small cap)",
    "QQQ": "Nasdaq-100",
    "AGG": "US Agg Bond",
}


def _fetch_benchmark_returns(start_date: str) -> dict[str, dict[str, float]]:
    """Fetch total-return % for benchmark tickers from start_date to today.

    Returns {ticker: {"start": float, "end": float, "return_pct": float}}.
    Returns {} on failure.
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return {}

    try:
        raw = yf.download(
            BENCHMARK_TICKERS,
            start=start_date,
            progress=False,
            group_by="ticker",
            threads=True,
            auto_adjust=True,  # auto_adjust=True bakes dividends into Close
        )
    except Exception as exc:
        logger.warning("benchmark fetch failed: %s", exc)
        return {}

    out: dict[str, dict[str, float]] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for t in BENCHMARK_TICKERS:
            try:
                closes = raw[t]["Close"].dropna()
                if len(closes) < 2:
                    continue
                start = float(closes.iloc[0])
                end = float(closes.iloc[-1])
                out[t] = {
                    "start": start,
                    "end": end,
                    "return_pct": (end - start) / start * 100,
                }
            except (KeyError, IndexError):
                continue
    return out


def _render_benchmarks(
    gen_meta: dict[str, Any],
    equity: dict[str, list[dict[str, Any]]],
    no_prices: bool,
) -> str:
    if no_prices:
        return ""
    start_date = (gen_meta.get("created_at") or "")[:10]
    if not start_date:
        return ""

    bench = _fetch_benchmark_returns(start_date)
    if not bench:
        return ""

    # Capital-weighted gen return for comparison
    latest = _latest_equity(equity)
    weighted_num = 0.0
    weighted_den = 0.0
    for snap in latest.values():
        ret = snap.get("total_return_pct")
        cap = snap.get("total_capital") or 0
        if ret is not None and cap:
            weighted_num += ret * cap
            weighted_den += cap
    portfolio_return = (weighted_num / weighted_den) if weighted_den else None
    spy_return = bench.get("SPY", {}).get("return_pct")

    # Benchmark comparison table
    head = '<tr><th>Index</th><th>Ticker</th><th class="num">Return</th><th class="num">vs Portfolio</th></tr>'
    body_rows = []
    # Portfolio row
    if portfolio_return is not None:
        cls = "pos" if portfolio_return > 0 else ("neg" if portfolio_return < 0 else "")
        body_rows.append(
            "<tr>"
            "<td><strong>Portfolio (cap-weighted)</strong></td>"
            "<td>—</td>"
            f'<td class="num {cls}">{_fmt_pct(portfolio_return)}</td>'
            '<td class="num">—</td>'
            "</tr>"
        )
    for t in BENCHMARK_TICKERS:
        b = bench.get(t)
        if not b:
            continue
        ret = b["return_pct"]
        excess = (portfolio_return - ret) if portfolio_return is not None else None
        ret_cls = "pos" if ret > 0 else ("neg" if ret < 0 else "")
        ex_cls = "pos" if (excess or 0) > 0 else ("neg" if (excess or 0) < 0 else "")
        body_rows.append(
            "<tr>"
            f"<td>{escape(BENCHMARK_LABELS.get(t, t))}</td>"
            f"<td><code>{escape(t)}</code></td>"
            f'<td class="num {ret_cls}">{_fmt_pct(ret)}</td>'
            f'<td class="num {ex_cls}">{_fmt_pct(excess) if excess is not None else "—"}</td>'
            "</tr>"
        )
    table = (
        '<table class="data-table">'
        f"<thead>{head}</thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )

    # Excess return vs SPY heatmap (per cohort)
    excess_heatmap_html = ""
    if spy_return is not None:
        excess_data: dict[str, dict[str, float | None]] = {}
        for h in ch.HORIZON_LABELS:
            excess_data[h] = {}
            for s in ch.SIZE_KEYS:
                name = f"horizon_{h}_size_{s}"
                snap = latest.get(name) or {}
                ret = _safe_num(snap.get("total_return_pct"))
                excess_data[h][s] = (ret - spy_return) if ret is not None else None

        has_any = any(v is not None for row in excess_data.values() for v in row.values())
        if has_any:
            fig = ch.make_cohort_heatmap(excess_data, "Excess Return vs SPY (pp)")
            b64 = _chart_to_png_b64(fig, height=380)
            excess_heatmap_html = (
                f'<h3 style="margin-top:18px;font-size:13px;color:#9ca3af;">'
                f'Excess return vs SPY (over {start_date} → today, percentage points)</h3>'
                f'{_img_tag(b64, "Excess return vs SPY heatmap")}'
            )

    period_note = (
        f'<p class="muted" style="margin-top:0;font-size:12px;">'
        f'Period: {escape(start_date)} → today · Total returns include dividends (auto-adjusted)</p>'
    )

    return f"""
    <section>
      <h2>Benchmarks &amp; alpha</h2>
      {period_note}
      {table}
      {excess_heatmap_html}
    </section>
    """


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------

_CSS = """
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: #0e1117;
  color: #fafafa;
  font-size: 14px;
  line-height: 1.5;
}
.wrap { max-width: 800px; margin: 0 auto; padding: 24px 16px; }
.title-bar { padding: 0 0 16px; border-bottom: 1px solid #2d3139; margin-bottom: 24px; }
.title-bar h1 { font-size: 22px; margin: 0; color: #fafafa; }
.title-bar .subtitle { color: #9ca3af; font-size: 13px; margin-top: 4px; }
.gen-block { margin-bottom: 48px; }
.gen-sep { border: 0; border-top: 1px dashed #2d3139; margin: 36px 0; }
.header { background: #1a1d24; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.header-top { display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 20px; margin: 0; color: #fafafa; }
.header-meta { color: #9ca3af; font-size: 12px; margin-top: 6px; }
.header-meta span { margin-right: 12px; }
.header-desc { color: #d1d5db; margin: 8px 0 0; font-size: 13px; }
.badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.badge-green { background: rgba(34,197,94,0.15); color: #22c55e; border: 1px solid rgba(34,197,94,0.3); }
.badge-amber { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.3); }
.badge-red { background: rgba(239,68,68,0.15); color: #ef4444; border: 1px solid rgba(239,68,68,0.3); }
.badge-muted { background: rgba(107,114,128,0.15); color: #9ca3af; border: 1px solid rgba(107,114,128,0.3); }
.kpi-row { display: table; width: 100%; border-spacing: 8px; margin-bottom: 16px; }
.kpi-tile { display: table-cell; background: #1a1d24; border-radius: 6px; padding: 12px; width: 25%; }
.kpi-label { color: #9ca3af; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-value { font-size: 20px; font-weight: 600; color: #fafafa; margin: 4px 0; }
.kpi-sub { color: #6b7280; font-size: 11px; }
section { background: #1a1d24; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
section h2 { font-size: 15px; margin: 0 0 12px; color: #fafafa; font-weight: 600; }
section img { max-width: 100%; height: auto; border-radius: 4px; display: block; }
.placeholder { color: #6b7280; font-style: italic; padding: 24px; text-align: center; background: #0e1117; border-radius: 4px; }
.muted { color: #6b7280; font-style: italic; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #2d3139; text-align: left; vertical-align: top; }
th { color: #9ca3af; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; background: #14171d; }
tbody tr:nth-child(odd) { background: rgba(20,23,29,0.5); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.row-head { background: #14171d; color: #d1d5db; font-weight: 600; }
.matrix-table td { text-align: center; }
.matrix-table .cell-ret { font-weight: 600; font-size: 13px; }
.matrix-table .cell-sub { color: #6b7280; font-size: 10px; margin-top: 2px; }
.pos { color: #22c55e; }
.neg { color: #ef4444; }
.footer { color: #6b7280; font-size: 11px; text-align: center; margin-top: 32px; padding-top: 16px; border-top: 1px solid #2d3139; }
"""


def _render_one_generation(
    gen_meta: dict[str, Any],
    date: str,
    no_prices: bool,
) -> str:
    gen_id = gen_meta["gen_id"]
    state_dir = gen_meta["state_dir"]

    def _safe(fn, default, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("loader %s failed for %s: %s", fn.__name__, gen_id, exc)
            return default

    metrics = _safe(dl.load_cohort_metrics, {"cohorts": {}, "per_strategy": {}}, gen_id, state_dir)
    equity = _safe(dl.load_equity_history, {}, gen_id, state_dir)
    regime = _safe(dl.load_regime_history, [], gen_id, state_dir)
    signal_stats = _safe(dl.load_signal_stats, {"per_strategy": {}, "total_signals": 0, "total_traded": 0}, gen_id, state_dir)
    capital = _safe(dl.load_capital_deployment, [], gen_id, state_dir)

    if no_prices:
        # Bypass yfinance: use entry price as current
        from datetime import datetime
        trades = dl.load_all_trades(gen_id, state_dir)
        positions = []
        strategy_pnl_map: dict[str, dict[str, Any]] = {}
        today = datetime.now().date()
        for t in trades:
            entry = float(t.get("entry_price", 0) or 0)
            shares = float(t.get("shares", 0) or 0)
            direction = t.get("direction", "long")
            status = t.get("status", "open")
            if status == "closed":
                current = float(t.get("exit_price", 0) or 0)
            else:
                current = entry
            pnl = (entry - current) * shares if direction == "short" else (current - entry) * shares
            cost = entry * shares if entry > 0 else 1
            positions.append({
                "cohort": t.get("cohort", ""),
                "horizon": t.get("horizon", ""),
                "size": t.get("size", ""),
                "ticker": t.get("ticker", ""),
                "strategy": t.get("strategy", ""),
                "direction": direction,
                "entry_price": entry,
                "current_price": current,
                "shares": shares,
                "position_value": shares * current * (1 if direction == "long" else -1),
                "pnl": pnl,
                "pnl_pct": (pnl / cost * 100) if cost > 0 else 0,
                "status": status,
                "days_held": 0,
                "entry_date": t.get("entry_date", ""),
            })
        # Aggregate to strategy_pnl
        for p in positions:
            s = p["strategy"] or "unknown"
            d = strategy_pnl_map.setdefault(s, {
                "strategy": s,
                "realized_long": 0.0, "realized_short": 0.0,
                "unrealized_long": 0.0, "unrealized_short": 0.0,
                "open_long_count": 0, "open_short_count": 0,
                "closed_count": 0,
            })
            if p["status"] == "closed":
                if p["direction"] == "short":
                    d["realized_short"] += p["pnl"]
                else:
                    d["realized_long"] += p["pnl"]
                d["closed_count"] += 1
            else:
                if p["direction"] == "short":
                    d["unrealized_short"] += p["pnl"]
                    d["open_short_count"] += 1
                else:
                    d["unrealized_long"] += p["pnl"]
                    d["open_long_count"] += 1
        strategy_pnl = sorted(strategy_pnl_map.values(), key=lambda r: (
            r["realized_long"] + r["realized_short"]
            + r["unrealized_long"] + r["unrealized_short"]
        ), reverse=True)
        for r in strategy_pnl:
            r["total_pnl"] = (
                r["realized_long"] + r["realized_short"]
                + r["unrealized_long"] + r["unrealized_short"]
            )
    else:
        positions = _safe(dl.load_position_pnl, [], gen_id, state_dir)
        strategy_pnl = _safe(dl.load_strategy_pnl, [], gen_id, state_dir)

    # Sanitize NaN/inf P&L values that occasionally appear in real state.
    import math
    def _is_bad_num(v):
        return isinstance(v, float) and (math.isnan(v) or math.isinf(v))

    for p in positions:
        for k in ("pnl", "pnl_pct", "current_price", "position_value"):
            if _is_bad_num(p.get(k)):
                p[k] = 0.0
    for r in strategy_pnl:
        for k in ("realized_long", "realized_short", "unrealized_long", "unrealized_short", "total_pnl"):
            if _is_bad_num(r.get(k)):
                r[k] = 0.0

    current_regime = (regime[-1].get("overall_regime") if regime else None)

    parts = [
        '<div class="gen-block">',
        _render_header(gen_meta, date, current_regime),
        _render_kpis(metrics, capital, signal_stats, positions, equity),
        _render_cohort_matrix(metrics, equity),
        _render_benchmarks(gen_meta, equity, no_prices),
        _render_equity_curves(equity),
        _render_strategy_pnl(strategy_pnl),
        _render_winners_losers(positions),
        _render_positions_table(positions),
        _render_regime_timeline(regime),
        '</div>',
    ]
    return "".join(parts)


def render_dashboard_html(
    gen_metadatas: Iterable[dict[str, Any]],
    date: str,
    no_prices: bool = False,
) -> str:
    """Render the full HTML email body for the given generations."""
    gens = list(gen_metadatas)
    if not gens:
        body = '<p class="muted">No active generations to report.</p>'
    else:
        blocks = [_render_one_generation(g, date, no_prices) for g in gens]
        body = '<hr class="gen-sep" />'.join(blocks)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>EventEdge Daily — {html.escape(date)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="title-bar">
    <h1>EventEdge Daily</h1>
    <div class="subtitle">{html.escape(date)} · {len(gens)} generation(s)</div>
  </div>
  {body}
  <div class="footer">Generated from data/generations state · forward via Gmail to read on mobile</div>
</div>
</body>
</html>"""


def suppress_streamlit_warnings() -> None:
    """Re-apply Streamlit warning suppression. Already runs at import time."""
    _silence_streamlit()
