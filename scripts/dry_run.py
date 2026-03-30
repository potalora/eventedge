"""Dry-run: 1 generation with minimal parameters to validate every stage."""

import json
import os
import sys
import time
import tempfile
from copy import deepcopy
from datetime import datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.storage.db import Database

# ─── Configuration ───────────────────────────────────────────────────────────
config = deepcopy(DEFAULT_CONFIG)
config["autoresearch"].update({
    "max_generations": 1,
    "strategies_per_generation": 4,   # ask for 4, some will pass CRO
    "tickers_per_strategy": 2,        # only 2 tickers each
    "walk_forward_windows": 1,        # 1 window (fewer pipeline calls)
    "holdout_weeks": 4,
    "budget_cap_usd": 50.0,
    "universe": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
})

# Use Haiku everywhere to minimize cost
config["llm_provider"] = "anthropic"
config["autoresearch"]["strategist_model"] = "claude-haiku-4-5-20251001"
config["autoresearch"]["cro_model"] = "claude-haiku-4-5-20251001"
config["autoresearch"]["cache_model"] = "claude-haiku-4-5-20251001"

# ─── Database ────────────────────────────────────────────────────────────────
results_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
os.makedirs(results_dir, exist_ok=True)
db_path = os.path.join(results_dir, "dry_run.db")

# Start fresh
if os.path.exists(db_path):
    os.remove(db_path)

db = Database(db_path)

# ─── Event logging ───────────────────────────────────────────────────────────
stage_num = [0]
start_time = time.time()

def on_event(event):
    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    kind = event.kind
    msg = event.data.get("message", "")

    # Highlight phase transitions
    if kind in ("phase", "generation_start", "generation_done", "complete", "interrupted"):
        stage_num[0] += 1
        print(f"\n{'='*70}")
        print(f"  STAGE {stage_num[0]}  [{mins}m{secs:02d}s]  {kind}: {msg}")
        print(f"{'='*70}")
    elif kind == "step":
        step = event.data.get("step", "")
        print(f"  [{mins}m{secs:02d}s] {step}: {msg}")
    else:
        print(f"  [{mins}m{secs:02d}s] {kind}: {msg}")

    # Print extra detail for key events
    if kind == "step" and event.data.get("step") == "ranked":
        rankings = event.data.get("rankings", [])
        for name, score in rankings:
            print(f"           {name}: {score:.4f}")

    if kind == "step" and event.data.get("step") == "propose_done":
        names = event.data.get("names", [])
        for n in names:
            print(f"           → {n}")

# ─── Run ─────────────────────────────────────────────────────────────────────
print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║                  AUTORESEARCH DRY RUN (1 generation)               ║
╠══════════════════════════════════════════════════════════════════════╣
║  Strategies per gen:  2                                            ║
║  Tickers per strat:   2                                            ║
║  Walk-forward windows: 1                                           ║
║  Universe:            AAPL, MSFT, NVDA, GOOGL, AMZN               ║
║  Models:              All Haiku (cost-optimized)                   ║
║  DB:                  {db_path:<51s}║
╚══════════════════════════════════════════════════════════════════════╝
""")

import logging
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("peewee").setLevel(logging.WARNING)

from tradingagents.autoresearch.evolution import EvolutionEngine

engine = EvolutionEngine(db, config, on_event=on_event)

try:
    result = engine.run()
except Exception as e:
    print(f"\n❌ ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    result = None

# ─── Results ─────────────────────────────────────────────────────────────────
elapsed = time.time() - start_time
mins, secs = divmod(int(elapsed), 60)

print(f"\n{'='*70}")
print(f"  DRY RUN COMPLETE  [{mins}m{secs:02d}s]")
print(f"{'='*70}")

if result:
    print(f"\n  Generations run: {result.get('generations_run', 0)}")
    print(f"  Budget used:     ${result.get('budget_used', 0):.2f}")
    print(f"  Interrupted:     {result.get('interrupted', False)}")

    cache = result.get("cache_stats", {})
    print(f"  Cache:           {cache.get('hits', 0)} hits, {cache.get('misses', 0)} misses "
          f"({cache.get('hit_rate', 0):.0%} hit rate)")

    lb = result.get("leaderboard", [])
    if lb:
        print(f"\n  LEADERBOARD:")
        for entry in lb:
            print(f"    #{entry['rank']} {entry['name']:<30s} "
                  f"fitness={entry['fitness_score']:.4f}  "
                  f"status={entry['status']}")
    else:
        print("\n  No strategies on leaderboard.")

# Verify DB contents
print(f"\n  DB VERIFICATION:")
strategies = db.get_top_strategies(limit=10)
print(f"    Strategies in DB:    {len(strategies)}")
reflections = db.get_reflections()
print(f"    Reflections in DB:   {len(reflections)}")
weights = db.get_analyst_weights()
print(f"    Analyst weights:     {len(weights)}")

if reflections:
    ref = reflections[-1]
    print(f"\n  LATEST REFLECTION (gen {ref['generation']}):")
    for p in ref.get("patterns_that_work", [])[:3]:
        print(f"    ✓ {p}")
    for p in ref.get("patterns_that_fail", [])[:3]:
        print(f"    ✗ {p}")

db.close()
print(f"\n  Done. DB saved to: {db_path}")
