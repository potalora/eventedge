"""Render the immutable metric-v2 generation report as a self-contained email."""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

from tradingagents.dashboard import data_loaders as dl

HEADLINE_TITLE = "Four $100k horizon books"
PANEL_LABEL = "Equal-weighted scenario panel"
DEPENDENCE_DISCLOSURE = (
    "Dependent scenario portfolios: shared signals and market data mean the "
    "books are not independent observations and are not combined fund AUM."
)
STRESS_TEST_LABEL = "$5k/$10k/$50k concentration stress tests"
SHARPE_LABEL = "Annualized daily net Sharpe"
INFORMATION_RATIO_LABEL = "Annualized matched-benchmark information ratio"
ACCURACY_LABEL = "Directional accuracy (5 XNYS sessions)"
INSUFFICIENT_HISTORY = "Insufficient history (<30 valid sessions)"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2%}"


def _metric(value: Any, *, insufficient: bool = False) -> str:
    if value is None:
        return INSUFFICIENT_HISTORY if insufficient else "—"
    return f"{float(value):.2f}"


def _book_rows(books: dict[str, Any]) -> str:
    rows = []
    for cohort_id, book in sorted(books.items()):
        rows.append(
            "<tr>"
            f"<td>{escape(cohort_id)}</td>"
            f"<td>{_pct(book.get('total_return'))}</td>"
            f"<td>{_metric(book.get('annualized_daily_net_sharpe'), insufficient=True)}</td>"
            f"<td>{_metric(book.get('annualized_matched_information_ratio'), insufficient=True)}</td>"
            f"<td>{_pct(book.get('directional_accuracy_5d'))}</td>"
            f"<td>{escape(str(book.get('valid_sessions', '—')))}</td>"
            "</tr>"
        )
    return (
        "".join(rows)
        or '<tr><td colspan="6">No governed metric books available.</td></tr>'
    )


def _evidence_rows(books: dict[str, Any]) -> str:
    rows = []
    for cohort_id, book in sorted(books.items()):
        rows.append(
            "<tr>"
            f"<td>{escape(cohort_id)}</td><td>{escape(str(book.get('valuation_at', 'Unavailable')))}</td>"
            f"<td>{escape(str(book.get('benchmark_at', 'Unavailable')))}</td>"
            f"<td>{_pct(book.get('matched_benchmark_return'))}</td>"
            f"<td>{_metric(book.get('gross_weight'))}</td><td>{_metric(book.get('net_weight'))}</td>"
            f"<td>{escape(str(book.get('cumulative_costs', 'Unavailable')))}</td>"
            "</tr>"
        )
    return (
        "".join(rows)
        or '<tr><td colspan="7">No governed metric evidence available.</td></tr>'
    )


def _benchmark_rows(series: dict[str, Any]) -> str:
    rows = []
    for cohort_id, item in sorted(series.items()):
        benchmarks = item.get("benchmarks", {}) if isinstance(item, dict) else {}
        for symbol in ("SPY", "BIL"):
            observations = (
                benchmarks.get(symbol, []) if isinstance(benchmarks, dict) else []
            )
            latest = observations[-1] if observations else {}
            rows.append(
                f"<tr><td>{escape(cohort_id)}</td><td>{symbol}</td>"
                f"<td>{escape(str(latest.get('observed_at', 'Unavailable')))}</td>"
                f"<td>{escape(str(latest.get('close', 'Unavailable')))}</td></tr>"
            )
    return (
        "".join(rows)
        or '<tr><td colspan="4">Persisted SPY/BIL observations unavailable.</td></tr>'
    )


def _report_section(report: dict[str, Any]) -> str:
    epoch = report.get("epoch") or {}
    panel = report.get("scenario_panel")
    panel_value = _pct(panel.get("total_return")) if panel else "Unavailable"
    panel_note = (
        ""
        if panel
        else escape(str(report.get("scenario_panel_unavailable_reason", "unavailable")))
    )
    headline = dict(report.get("headline_books", {}) or {})
    stress = dict(report.get("stress_tests", {}) or {})
    series = dict(report.get("cohort_series", {}) or {})
    epoch_id = epoch.get("epoch_id", "No available metric epoch")
    return f"""
    <section>
      <h2>{HEADLINE_TITLE}</h2>
      <p><strong>{PANEL_LABEL}:</strong> {panel_value} {panel_note}</p>
      <p class="muted">{DEPENDENCE_DISCLOSURE}</p>
      <p class="muted">Metric epoch: {escape(str(epoch_id))}; Schema v2.</p>
      <table><thead><tr><th>Book</th><th>Net return</th><th>{SHARPE_LABEL}</th><th>{INFORMATION_RATIO_LABEL}</th><th>{ACCURACY_LABEL}</th><th>Valid sessions</th></tr></thead>
      <tbody>{_book_rows(headline)}</tbody></table>
    </section>
    <section>
      <h2>{STRESS_TEST_LABEL}</h2>
      <p class="muted">These are concentration stress tests, not combined capital or fund AUM.</p>
      <table><thead><tr><th>Book</th><th>Net return</th><th>{SHARPE_LABEL}</th><th>{INFORMATION_RATIO_LABEL}</th><th>{ACCURACY_LABEL}</th><th>Valid sessions</th></tr></thead>
      <tbody>{_book_rows(stress)}</tbody></table>
    </section>
    <section>
      <h2>Persisted valuation, benchmark, exposure, and cost evidence</h2>
      <table><thead><tr><th>Book</th><th>Valuation timestamp</th><th>Benchmark timestamp</th><th>Matched benchmark return</th><th>Gross exposure</th><th>Net exposure</th><th>Costs</th></tr></thead>
      <tbody>{_evidence_rows({**headline, **stress})}</tbody></table>
      <h3>Persisted SPY/BIL observations</h3>
      <table><thead><tr><th>Book</th><th>Benchmark</th><th>Benchmark timestamp</th><th>Close</th></tr></thead>
      <tbody>{_benchmark_rows(series)}</tbody></table>
    </section>"""


def _render_one_generation(gen_meta: dict[str, Any], date: str) -> str:
    report = gen_meta.get("metric_report")
    if report is None:
        report = dl.load_generation_metrics(gen_meta["gen_id"], gen_meta["state_dir"])
    description = escape(str(gen_meta.get("description", "")))
    return f'<div class="gen"><h1>{escape(str(gen_meta.get("gen_id", "?")))}</h1><p>{escape(date)} {description}</p>{_report_section(report)}</div>'


def render_dashboard_html(
    gen_metadatas: Iterable[dict[str, Any]], date: str, no_prices: bool = False
) -> str:
    """Render only persisted metric-v2 evidence; ``no_prices`` is retained for API compatibility."""
    gens = list(gen_metadatas)
    body = (
        "".join(_render_one_generation(item, date) for item in gens)
        if gens
        else "<p>No active generations to report.</p>"
    )
    return f"""<!doctype html><html><head><title>EventEdge Daily — {escape(date)}</title><style>
body{{background:#0e1117;color:#fafafa;font:14px sans-serif;margin:auto;max-width:900px;padding:24px}} section,.gen{{background:#1a1d24;padding:16px;margin:16px 0;border-radius:8px}} table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #333;text-align:left}}.muted{{color:#a1a1aa}}
</style></head><body><h1>EventEdge Daily</h1>{body}</body></html>"""


def suppress_streamlit_warnings() -> None:
    """Compatibility no-op; this module does not initialize Streamlit."""
