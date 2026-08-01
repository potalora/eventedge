from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradingagents.strategies.metrics.models import LEGACY_SCHEMA_LABEL

_LEGACY_GENERATIONS = ("gen_001", "gen_002", "gen_003")
_LEGACY_REASON = "legacy_same_bar_close_and_unreconciled_costs"


def build_legacy_registry(manifest: dict) -> dict:
    present = {
        item.get("gen_id")
        for item in manifest.get("generations", [])
        if isinstance(item, dict)
    }
    return {
        generation_id: {
            "metric_schema": LEGACY_SCHEMA_LABEL,
            "promotion_eligible": False,
            "reason": _LEGACY_REASON,
        }
        for generation_id in _LEGACY_GENERATIONS
        if generation_id in present
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the metrics-v2 legacy generation registry."
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    registry = build_legacy_registry(manifest)
    rendered = json.dumps(registry, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.write:
        Path(args.output).write_text(rendered)


if __name__ == "__main__":
    main()

