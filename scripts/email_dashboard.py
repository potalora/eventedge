#!/usr/bin/env python3
"""Render the EventEdge dashboard as a self-contained HTML file you can email yourself.

Usage:
    .venv/bin/python scripts/email_dashboard.py [--gen GEN_ID] [--date YYYY-MM-DD]
                                                [--out PATH] [--no-prices]

Defaults: all active generations, today's date, data/dashboard_emails/<date>.html.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dashboard import email_export


def _load_manifest_generations() -> list[dict]:
    path = REPO_ROOT / "data" / "generations" / "manifest.json"
    if not path.exists():
        print(f"ERROR: manifest not found at {path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text())
    return data.get("generations", [])


def _select_generations(args_gens: list[str]) -> list[dict]:
    all_gens = _load_manifest_generations()
    if args_gens:
        index = {g["gen_id"]: g for g in all_gens}
        missing = [g for g in args_gens if g not in index]
        if missing:
            print(f"ERROR: generation(s) not found in manifest: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        return [index[g] for g in args_gens]
    active = [g for g in all_gens if g.get("status") == "active"]
    if not active:
        print("ERROR: no active generations in manifest. Pass --gen to select one explicitly.", file=sys.stderr)
        sys.exit(1)
    return active


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gen", action="append", default=[], help="Generation ID (repeatable). Default: all active.")
    parser.add_argument("--date", default=None, help="Snapshot date label, YYYY-MM-DD. Default: today.")
    parser.add_argument("--out", default=None, help="Output HTML path. Default: data/dashboard_emails/<date>.html.")
    parser.add_argument("--no-prices", action="store_true", help="Skip yfinance price fetch (mark open positions at entry).")
    args = parser.parse_args()

    email_export.suppress_streamlit_warnings()

    date = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = REPO_ROOT / "data" / "dashboard_emails" / f"{date}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    gens = _select_generations(args.gen)
    print(f"Rendering {len(gens)} generation(s): {', '.join(g['gen_id'] for g in gens)}", file=sys.stderr)

    html = email_export.render_dashboard_html(gens, date, no_prices=args.no_prices)
    out_path.write_text(html, encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
