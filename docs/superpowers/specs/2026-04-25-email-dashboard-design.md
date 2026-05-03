# Email-able Dashboard Snapshot — Design

**Date:** 2026-04-25
**Status:** Approved (design phase)

## Goal

Generate a self-contained HTML file that mirrors the EventEdge Streamlit dashboard so it can be opened in a browser and forwarded via Gmail. No SMTP, no live Streamlit server required.

## Non-Goals

- Sending email automatically (deferred — user does not have Gmail SMTP configured).
- Replacing the live Streamlit dashboard.
- Replacing the existing markdown daily reports in `docs/reports/`.

## Entry Point

```
.venv/bin/python scripts/email_dashboard.py [--gen GEN_ID] [--date YYYY-MM-DD] [--out PATH] [--no-prices]
```

- `--gen` — generation ID. May be repeated. Default: all active generations from the manifest.
- `--date` — snapshot date label shown in the email header. Default: today (US/Eastern).
- `--out` — output path. Default: `data/dashboard_emails/<date>.html`.
- `--no-prices` — skip yfinance price fetch; mark open positions at entry price. Useful offline.

On success the script prints the absolute path of the written file. Exit code 0 on success, 1 on fatal errors (no active gens, no manifest).

## Files Added

| Path | Purpose |
|------|---------|
| `scripts/email_dashboard.py` | CLI entry point: argparse, error handling, calls renderer, writes file |
| `tradingagents/dashboard/email_export.py` | Pure-Python rendering module (no Streamlit import) |
| `tests/test_email_dashboard.py` | Unit tests with synthetic state fixture |

No existing files are modified except `pyproject.toml` (dependency add).

## Data Access

Reuse the existing `tradingagents/dashboard/data_loaders.py` functions — they already read all required state files and apply the deduplication logic the dashboard relies on. They are decorated with `@st.cache_data`, but the decorated callables work fine when called outside a Streamlit script run (no script context → cache is a passthrough). `streamlit` is already a project dependency, so importing it costs nothing at runtime.

Functions used:

| data_loaders function | Purpose |
|----------------------|---------|
| `get_active_generations()` | Manifest filter when `--gen` not given |
| `get_all_generations()` | Validation when `--gen X` given |
| `load_cohort_metrics(gen_id, gen_state_dir)` | Per-cohort comparison metrics |
| `load_cohort_heatmap(gen_id, gen_state_dir, metric)` | Heatmap z-values for return % |
| `load_equity_history(gen_id, gen_state_dir)` | Per-cohort equity snapshots from `equity_snapshots.jsonl` |
| `load_regime_history(gen_id, gen_state_dir)` | Deduped regime timeline from `regime_snapshots.json` |
| `load_signal_stats(gen_id, gen_state_dir)` | Per-strategy signal/hit stats |
| `load_position_pnl(gen_id, gen_state_dir)` | Open + closed positions, marked to latest yfinance close |
| `load_strategy_pnl(gen_id, gen_state_dir)` | P&L attribution by strategy |
| `load_capital_deployment(gen_id, gen_state_dir)` | Capital deployed per cohort |

Top-level state paths (reference, not directly read in `email_export.py`):

- `data/generations/manifest.json` — generation registry.
- `data/generations/<gen>/horizon_<h>_size_<s>/equity_snapshots.jsonl` — daily equity rows.
- `data/generations/<gen>/horizon_<h>_size_<s>/regime_snapshots.json` — VIX/regime per run.
- `data/generations/<gen>/horizon_<h>_size_<s>/paper_trades.json` — open & closed trades.
- `data/generations/<gen>/horizon_<h>_size_<s>/signal_journal.jsonl` — signal records.

If a state file is missing for some cohort, the loader silently skips that cohort. If all state is missing for a generation, sections that need data render "no data" placeholders rather than failing.

Network: `load_position_pnl` calls `yfinance` to mark open positions. Failure is already handled (returns `{}` and entries fall back to entry price). The CLI accepts `--no-prices` flag to skip this for offline use.

## Rendering

### Module API

```python
def render_dashboard_html(gen_ids: list[str], date: str) -> str: ...
```

Returns a complete HTML document as a string.

### Charts → PNG → base64

```python
def _chart_to_png_b64(fig: go.Figure, width: int = 800, height: int | None = None) -> str:
    """Render a plotly figure to base64 PNG via kaleido. Returns '' on failure."""
```

Wraps `fig.to_image(format="png", width=width, height=height, scale=2)` (kaleido). Caught exceptions log a warning and return `""`. Caller substitutes a `<p class="placeholder">[chart unavailable]</p>`.

### Section renderers

Each takes the loaded gen data and returns an HTML fragment:

- `_render_header(gen_meta) -> str` — gen ID, date, days running, regime badge.
- `_render_kpis(metrics) -> str` — 4-tile row.
- `_render_cohort_matrix(metrics, heatmap_data) -> str` — PNG heatmap + trade-count table.
- `_render_equity_curves(history) -> str` — facet PNG.
- `_render_strategy_pnl(strategy_rows) -> str` — PNG + table.
- `_render_winners_losers(positions) -> str` — PNG.
- `_render_positions_table(positions) -> str` — top 50 by abs(P&L).
- `_render_regime_timeline(snapshots) -> str` — PNG at bottom.

Top-level `render_dashboard_html` glues sections per generation, separated by `<hr class="gen-sep">`.

### Reused chart functions

All chart constructors come from existing `tradingagents/dashboard/charts.py`:

| Section | Function |
|---------|----------|
| Cohort matrix | `make_cohort_heatmap` |
| Equity curves | `make_equity_curves_facet` |
| Strategy P&L | `make_strategy_pnl_chart` |
| Winners & losers | `make_winners_losers_bars` |
| Regime timeline | `make_regime_timeline` |

We do not reuse `make_capital_bars`, `make_strategy_bars`, `make_gen_comparison`, `make_drawdown_chart`, `make_position_treemap` for v1 — kept minimal.

## HTML / CSS

- Single inline `<style>` block, no external resources (Gmail strips them).
- Dark theme matching dashboard: `--bg: #0e1117`, `--panel: #1a1d24`, `--text: #fafafa`, `--muted: #9ca3af`, accents `#3b82f6` / `#22c55e` / `#ef4444` / `#fbbf24`.
- Max content width 760px, centered. Mobile-readable.
- Tables: 1px solid `#2d3139` borders, zebra striping `#161920`, right-aligned numeric columns.
- Regime badge: pill with regime color (`normal=green`, `stressed=amber`, `crisis=red`).
- Title block at top: "EventEdge Daily — &lt;date&gt;".

## Dependencies

Add to `pyproject.toml`:

```
"kaleido>=0.2.1"
```

No other new dependencies.

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `data/generations/manifest.json` missing | Exit 1 with message |
| No active generations and `--gen` not given | Exit 1 with message |
| `--gen X` given but X not in manifest | Exit 1 with message naming X |
| Cohort state dir empty | KPIs render as "—", section shows "no data" |
| `kaleido` chart render raises | Log warning, substitute `[chart unavailable]` placeholder, continue |
| Output dir missing | Create it (`mkdir -p` equivalent) |

## Testing

`tests/test_email_dashboard.py`:

1. **Fixture**: pytest tmp_path-based synthetic gen state — one gen, 2 cohorts, 3 days of equity history, 2 open positions, 1 strategy with P&L. Patch `_BASE_DIR` or pass dir as arg.
2. **`test_renders_minimum_html`** — call `render_dashboard_html`, assert output contains:
   - `<title>` with date
   - "EventEdge Daily"
   - Generation ID
   - At least one `<table>`
   - At least one `data:image/png;base64,` (with kaleido mocked to return constant bytes)
   - No `{` or `}` from unclosed f-strings
3. **`test_missing_state_degrades`** — empty cohort dir, assert "no data" or "—" appears, no exception.
4. **`test_chart_failure_placeholder`** — patch `_chart_to_png_b64` to return `""`, assert `[chart unavailable]` appears.
5. **`test_cli_writes_file`** — invoke `scripts/email_dashboard.py` via subprocess on the fixture, assert file exists at expected path and is non-empty.

`kaleido` is mocked in tests to avoid the actual Chromium-based renderer running in CI.

## Future Work (not in this scope)

- Gmail SMTP sender (deferred — user will set up later).
- Cron / launchd scheduling once SMTP works.
- Multi-gen comparison overlays in the email.
- Dark/light theme toggle (Gmail clients vary).
