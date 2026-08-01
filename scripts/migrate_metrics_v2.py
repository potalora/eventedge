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


def _validate_write_target(
    manifest_path: Path,
    output_path: Path,
    manifest: dict,
) -> None:
    resolved_manifest = manifest_path.expanduser().resolve()
    resolved_output = output_path.expanduser().resolve()
    protected_roots = {resolved_manifest}
    for item in manifest.get("generations", []):
        if not isinstance(item, dict):
            continue
        for key in ("state_dir", "path"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                continue
            root = Path(value).expanduser()
            if not root.is_absolute():
                root = resolved_manifest.parent / root
            protected_roots.add(root.resolve())

    if any(
        resolved_output == root or root in resolved_output.parents
        for root in protected_roots
    ):
        raise ValueError("output is inside protected generation history")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the metrics-v2 legacy generation registry."
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    output_path = Path(args.output)
    manifest = json.loads(manifest_path.read_text())
    registry = build_legacy_registry(manifest)
    rendered = json.dumps(registry, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.write:
        try:
            _validate_write_target(manifest_path, output_path, manifest)
        except ValueError as error:
            parser.error(str(error))
        output_path.write_text(rendered)


if __name__ == "__main__":
    main()
