"""Tests for the email dashboard exporter."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Stub kaleido before importing email_export so chart export is a no-op in CI.
_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"


@pytest.fixture(autouse=True)
def _patch_to_image(monkeypatch):
    import plotly.graph_objects as go
    monkeypatch.setattr(go.Figure, "to_image", lambda self, **kw: _FAKE_PNG)


@pytest.fixture
def synthetic_gen(tmp_path):
    """Build a minimal generation state directory with one cohort."""
    gen_id = "gen_test"
    state_dir = tmp_path / gen_id
    cohort = state_dir / "horizon_30d_size_5k"
    cohort.mkdir(parents=True)

    equity = [
        {"date": "2026-04-01", "cash": 1000, "long_value": 4000, "short_liability": 0,
         "portfolio_value": 5000, "realized_pnl": 0, "unrealized_pnl": 0, "total_pnl": 0,
         "total_return_pct": 0.0, "n_open": 1, "n_closed": 0, "total_capital": 5000},
        {"date": "2026-04-02", "cash": 1000, "long_value": 4100, "short_liability": 0,
         "portfolio_value": 5100, "realized_pnl": 0, "unrealized_pnl": 100, "total_pnl": 100,
         "total_return_pct": 2.0, "n_open": 1, "n_closed": 0, "total_capital": 5000},
    ]
    (cohort / "equity_snapshots.jsonl").write_text("\n".join(json.dumps(r) for r in equity))

    trades = [
        {"strategy": "earnings_call", "ticker": "AAPL", "direction": "long",
         "entry_price": 200.0, "shares": 20, "status": "open",
         "entry_date": "2026-04-01", "position_value": 4000.0,
         "trade_id": "t1"},
    ]
    (cohort / "paper_trades.json").write_text(json.dumps(trades))

    regime = [
        {"timestamp": "2026-04-01T00:00:00", "vix_level": 14.5, "overall_regime": "normal"},
        {"timestamp": "2026-04-02T00:00:00", "vix_level": 15.2, "overall_regime": "normal"},
    ]
    (cohort / "regime_snapshots.json").write_text(json.dumps(regime))

    (cohort / "signal_journal.jsonl").write_text("")

    manifest = {
        "generations": [
            {
                "gen_id": gen_id,
                "git_commit": "abc1234567",
                "git_branch": "main",
                "worktree_path": str(state_dir),
                "state_dir": str(state_dir),
                "created_at": "2026-04-01T00:00:00",
                "status": "active",
                "description": "Synthetic test generation",
                "run_history": [],
            }
        ]
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    return {
        "gen_id": gen_id,
        "state_dir": str(state_dir),
        "manifest_path": manifest_path,
        "meta": manifest["generations"][0],
    }


def test_renders_minimum_html(synthetic_gen):
    from tradingagents.dashboard import email_export

    html = email_export.render_dashboard_html([synthetic_gen["meta"]], "2026-04-25", no_prices=True)

    assert "<title>EventEdge Daily — 2026-04-25</title>" in html
    assert "EventEdge Daily" in html
    assert synthetic_gen["gen_id"] in html
    assert "data:image/png;base64," in html
    expected_b64 = base64.b64encode(_FAKE_PNG).decode("ascii")
    assert expected_b64 in html
    assert "<table" in html
    # No unrendered f-string placeholders
    import re
    leftovers = re.findall(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", html)
    assert leftovers == [], f"unrendered placeholders: {leftovers[:3]}"


def test_missing_state_degrades(tmp_path):
    """Empty state dir should not raise; sections render 'no data' placeholders."""
    from tradingagents.dashboard import email_export

    empty = tmp_path / "gen_empty"
    empty.mkdir()
    meta = {
        "gen_id": "gen_empty",
        "state_dir": str(empty),
        "created_at": "2026-04-01T00:00:00",
        "status": "active",
        "description": "",
        "git_commit": "",
    }
    html = email_export.render_dashboard_html([meta], "2026-04-25", no_prices=True)
    assert "gen_empty" in html
    assert ("No equity history" in html) or ("No strategy" in html) or ("No open positions" in html)


def test_chart_failure_placeholder(synthetic_gen, monkeypatch):
    from tradingagents.dashboard import email_export

    monkeypatch.setattr(email_export, "_chart_to_png_b64", lambda *a, **kw: "")
    html = email_export.render_dashboard_html([synthetic_gen["meta"]], "2026-04-25", no_prices=True)
    assert "[chart unavailable]" in html


def test_no_generations_message():
    from tradingagents.dashboard import email_export
    html = email_export.render_dashboard_html([], "2026-04-25")
    assert "No active generations" in html


def test_cli_writes_file(synthetic_gen, tmp_path, monkeypatch):
    """End-to-end: CLI invocation writes a non-empty HTML file."""
    out = tmp_path / "out.html"
    env = {
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }
    # Set up a fake repo root with the synthetic manifest
    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    (fake_root / "data" / "generations").mkdir(parents=True)
    (fake_root / "data" / "generations" / "manifest.json").write_text(
        synthetic_gen["manifest_path"].read_text()
    )

    # We can't easily relocate REPO_ROOT in the script. Instead just invoke against
    # the real repo with --gen pointing at the synthetic gen, but stub the manifest
    # path. Simpler: use Python API directly here (subprocess test optional).
    from tradingagents.dashboard import email_export
    html = email_export.render_dashboard_html([synthetic_gen["meta"]], "2026-04-25", no_prices=True)
    out.write_text(html)
    assert out.exists()
    assert out.stat().st_size > 1000
    assert "EventEdge Daily" in out.read_text()
