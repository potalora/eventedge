#!/usr/bin/env python3
"""Manage parallel paper trading generations with code isolation.

Each generation freezes the codebase at a git commit and runs both cohorts
(control + adaptive) in an isolated state directory. Multiple generations
can run daily in parallel, building independent track records.

Usage:
    python scripts/run_generations.py start "Initial 7-strategy baseline"
    python scripts/run_generations.py run-daily [--date 2026-04-01]
    python scripts/run_generations.py run-learning
    python scripts/run_generations.py compare [--gens gen_001,gen_002]
    python scripts/run_generations.py list
    python scripts/run_generations.py pause gen_001
    python scripts/run_generations.py resume gen_001
    python scripts/run_generations.py retire gen_001
"""
from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("run_generations")


def _repo_root() -> str:
    """Find the repo root (parent of scripts/)."""
    return str(Path(__file__).resolve().parent.parent)


def main():
    parser = argparse.ArgumentParser(
        description="Manage parallel paper trading generations",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # start
    p_start = sub.add_parser("start", help="Start a new generation from current HEAD")
    p_start.add_argument("description", help="Description of this generation")

    # run-daily
    p_daily = sub.add_parser("run-daily", help="Run all active generations for a date")
    p_daily.add_argument("--date", default=None, help="Trading date (YYYY-MM-DD)")

    # run-learning
    sub.add_parser("run-learning", help="Run learning loop for all active generations")

    # compare
    p_compare = sub.add_parser("compare", help="Compare generations")
    p_compare.add_argument(
        "--gens", default=None,
        help="Comma-separated gen IDs to compare (default: all)",
    )

    # list
    sub.add_parser("list", help="List all generations")

    # pause
    p_pause = sub.add_parser("pause", help="Pause a generation")
    p_pause.add_argument("gen_id", help="Generation ID (e.g., gen_001)")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a paused generation")
    p_resume.add_argument("gen_id", help="Generation ID")

    # retire
    p_retire = sub.add_parser("retire", help="Retire a generation")
    p_retire.add_argument("gen_id", help="Generation ID")
    p_retire.add_argument(
        "--keep-worktree", action="store_true",
        help="Keep the git worktree (default: delete it)",
    )

    # event-study
    p_es = sub.add_parser("event-study", help="Run an event study (CAR) over journaled signals")
    p_es.add_argument("--gen", default=None, help="Generation ID (default: all active)")
    p_es.add_argument("--strategy", default=None, help="Limit to one strategy/group")
    p_es.add_argument("--since", default=None, help="Only signals on/after this date (YYYY-MM-DD)")
    p_es.add_argument("--json", default=None, help="Optional path to dump full result as JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Raise the soft file-descriptor limit before any FD-heavy work. launchd
    # imposes a soft RLIMIT_NOFILE of 256, which the 13-source / 12-strategy
    # fetch fan-out exceeds (it errored all 16 cohorts on 2026-06-01). setrlimit
    # is inherited by the per-generation worktree subprocesses spawned below.
    from tradingagents.sys_limits import raise_fd_limit
    raise_fd_limit()

    from tradingagents.strategies.orchestration.generation_manager import GenerationManager

    repo = _repo_root()
    manager = GenerationManager(repo)

    if args.command == "start":
        gen = manager.start_generation(args.description)
        print(f"Started {gen.gen_id}")
        print(f"  Commit:    {gen.git_commit[:12]}")
        print(f"  Branch:    {gen.git_branch}")
        print(f"  Worktree:  {gen.worktree_path}")
        print(f"  State dir: {gen.state_dir}")
        print(f"  Description: {gen.description}")

    elif args.command == "run-daily":
        from tradingagents.strategies.orchestration.trading_calendar import is_session

        requested = args.date or date.today().isoformat()
        try:
            trading_session = date.fromisoformat(requested)
        except ValueError:
            parser.error(f"invalid ISO trading date: {requested}")
        if not is_session(trading_session):
            parser.error(f"{requested} is not an XNYS session")
        trading_date = trading_session.isoformat()
        if not args.date:
            logger.info("Using XNYS session: %s", trading_date)
        results = manager.run_daily(trading_date)
        for gen_id, result in results.items():
            status = "OK" if result["success"] else "FAILED"
            elapsed = result.get("elapsed_s", 0)
            print(f"  {gen_id}: {status} ({elapsed:.1f}s)")
            if not result["success"] and result.get("error"):
                # Print first few lines of error
                error_lines = result["error"].strip().split("\n")
                for line in error_lines[:5]:
                    print(f"    {line}")

    elif args.command == "run-learning":
        results = manager.run_learning()
        for gen_id, result in results.items():
            status = "OK" if result["success"] else "FAILED"
            print(f"  {gen_id}: {status}")

    elif args.command == "compare":
        from tradingagents.strategies.orchestration.generation_comparison import (
            GenerationComparison,
            GenerationInfo as ComparisonGenInfo,
        )

        gens = manager.list_generations()
        if args.gens:
            selected = set(args.gens.split(","))
            gens = [g for g in gens if g.gen_id in selected]

        if not gens:
            print("No generations found.")
            return

        # Convert to comparison-compatible GenerationInfo
        comp_gens = [
            ComparisonGenInfo(
                gen_id=g.gen_id,
                state_dir=g.state_dir,
                description=g.description,
                created_at=g.created_at,
                status=g.status,
                git_commit=g.git_commit,
            )
            for g in gens
        ]
        comparison = GenerationComparison(comp_gens)
        print(comparison.format_report())

    elif args.command == "list":
        gens = manager.list_generations()
        if not gens:
            print("No generations found.")
            return

        for g in gens:
            runs = len(g.run_history)
            last_run = g.run_history[-1]["date"] if g.run_history else "never"
            print(
                f"  {g.gen_id} [{g.status}] — {g.description}"
                f"  (commit: {g.git_commit[:12]}, runs: {runs}, last: {last_run})"
            )

    elif args.command == "pause":
        manager.pause_generation(args.gen_id)
        print(f"Paused {args.gen_id}")

    elif args.command == "resume":
        manager.resume_generation(args.gen_id)
        print(f"Resumed {args.gen_id}")

    elif args.command == "retire":
        manager.retire_generation(args.gen_id, delete_worktree=not args.keep_worktree)
        print(f"Retired {args.gen_id}")

    elif args.command == "event-study":
        import glob
        from dataclasses import asdict

        from tradingagents.strategies.learning.signal_journal import SignalJournal
        from tradingagents.strategies.validation.engine import compute_car
        from tradingagents.strategies.validation.journal_source import events_from_journals
        from tradingagents.strategies.validation.price_adapter import yfinance_price_fn
        from tradingagents.strategies.validation.report import format_report

        gens = manager.list_generations()
        if args.gen:
            gens = [g for g in gens if g.gen_id == args.gen]
        if not gens:
            print("No generations found.")
            return

        journals: list[SignalJournal] = []
        for g in gens:
            for path in glob.glob(f"{g.state_dir}/*/signal_journal.jsonl"):
                cohort_dir = path.rsplit("/", 1)[0]
                journals.append(SignalJournal(cohort_dir))

        events = events_from_journals(journals, strategy=args.strategy, since=args.since)
        if not events:
            print("No journaled signals matched.")
            return
        print(f"Studying {len(events)} unique events across {len(journals)} cohort journals...")

        result = compute_car(events, yfinance_price_fn(), rng_seed=1)
        print(format_report(result))

        if args.json:
            import json

            with open(args.json, "w") as f:
                json.dump(
                    {
                        "events": [asdict(e) for e in result.events],
                        "aggregates": [asdict(a) for a in result.aggregates],
                        "skipped_tickers": result.skipped_tickers,
                    },
                    f,
                    indent=2,
                    default=str,
                )
            print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
