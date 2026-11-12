#!/usr/bin/env python3
"""Export the frozen e33cc9e admission comparison without importing EventEdge.

Only retained JSON is read. Production code, collectors, models, services and
portfolio databases are never loaded. Output must be outside the source checkout.
This deliberately supports the gen_025/e33cc9e evidence schema only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Any


SUPPORTED_COMMIT = "e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac"
STRATEGIES = ("earnings_call", "supply_chain", "govt_contracts")
HORIZONS = {"30d", "3m", "6m", "1y"}
UNIVERSE_EXCLUSIONS = {
    "outside_sip_exchange_universe", "inactive_asset", "absent_from_asset_master"
}
EVIDENCE_HASH_POLICY = {
    "algorithm": "sha256_canonical_json_sort_keys_separators_comma_colon_ensure_ascii",
    "supply_chain": "All retained article fields including issuer symbol, excluding observed_at.",
    "govt_contracts": "All retained contract fields including attribution evidence, excluding observed_at.",
    "earnings_call": (
        "Retained symbol, year, quarter, text_source, eps_actual, eps_estimate, "
        "revenue_actual, revenue_estimate, transcript_text, published_at. "
        "Excludes administrative acquisition_discovery_id and article_discovery_ids."
    ),
    "meaning": (
        "Different evidence hashes establish different retained evidence versions, "
        "not substantively new economic events."
    ),
}
NOTES = [
    "Frozen retrospective engineering comparison; neither session is a held-out or prospective evaluation. Source windows overlap.",
    "Rows preserve the complete screening qualifying population, original reasons and authoritative universe decisions. Acquisition calendar populations are retained separately.",
    "source_event_key groups exact native source identities. It does not assert common economic meaning across different articles or awards. Earnings fiscal identity includes issuer.",
    "evidence_sha256 hashes the full retained public evidence payload relevant to the source hypothesis excluding observation clock; earnings excludes administrative discovery references. New hashes are changed retained versions, not independently adjudicated substantive updates.",
    "Candidate availability and original source observation are distinct. Supply candidate metadata preserves publication time but omits raw source observed_at; the latter is retained here and must not be backdated.",
    "Held and pending membership is unknown: no verified predecision membership snapshot was established. Live portfolio state was not queried or treated as historical truth.",
    "Source article text is hashed but not reproduced. No account identifiers, balances, model outputs, or private reports are included.",
]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(kind: str, *parts: Any) -> str:
    """Exact stable-ID encoding from deployed execution/ids.py; no code import."""
    return kind + "_" + digest(parts)[:32]


def _unique_fields(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode(value: Any) -> Any:
    """Project typed JSON to built-ins; never construct runtime/pandas objects."""
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("$type")
    if tag == "mapping":
        result = {}
        for key, item in value["items"]:
            key = _decode(key)
            if key in result:
                raise ValueError("duplicate immutable mapping key")
            result[key] = _decode(item)
        return result
    if tag == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if tag in {"date", "datetime", "decimal"}:
        return value["value"]
    # Other typed data can occur in unrelated retained source fields. Keep its
    # inert JSON structure; the public projection below never includes it.
    return {key: _decode(item) for key, item in value.items()}


def aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} requires an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid {field} timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} requires timezone awareness")
    return parsed


def read_envelope(path: Path) -> tuple[dict, dict]:
    """Verify the payload digest over its original canonical typed encoding."""
    raw_bytes = path.read_bytes()
    raw = json.loads(raw_bytes, object_pairs_hook=_unique_fields)
    if not isinstance(raw, dict) or raw.get("$type") != "mapping":
        raise ValueError("unsupported source envelope encoding")
    raw_fields = dict(raw["items"])
    document = _decode(raw)
    if set(document) != {"version", "identity", "acquired_at", "payload", "digest"}:
        raise ValueError("unsupported source envelope fields")
    if document["version"] != "source-inputs-v1":
        raise ValueError("unsupported source envelope version")
    if digest(raw_fields["payload"]) != document["digest"]:
        raise ValueError("source payload digest mismatch")
    aware(document["acquired_at"], "envelope acquired_at")
    provenance = {
        "path": str(path),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "envelope_digest": document["digest"],
    }
    return document, provenance


def _event_key(strategy: str, ticker: str, native_identity: Any) -> str:
    family = {
        "earnings_call": "fiscal_period",
        "supply_chain": "news_article",
        "govt_contracts": "federal_award",
    }[strategy]
    return stable_id("event_key", strategy, family, ticker or "UNRESOLVED", native_identity)


def _published_at(article: dict) -> Any:
    published = article.get("published_at") or article.get("datetime")
    if isinstance(published, (int, float)) or (
        isinstance(published, str) and published.isdigit()
    ):
        return datetime.fromtimestamp(float(published), timezone.utc).isoformat()
    return published


def project_source(strategy: str, source: dict) -> tuple[str, dict]:
    """Bind native source and issuer identity before joining a discovery row."""
    observed = source.get("observed_at")
    if strategy == "earnings_call":
        ticker = source["symbol"]
        native = {"year": source["year"], "quarter": source["quarter"]}
        source_key = stable_id("source_event", strategy, ticker, native)
        fields = (
            "symbol", "year", "quarter", "text_source", "eps_actual", "eps_estimate",
            "revenue_actual", "revenue_estimate", "transcript_text", "published_at",
            "article_discovery_ids", "acquisition_discovery_id",
        )
        payload = {key: source[key] for key in fields if key in source}
        public_source = {key: value for key, value in payload.items() if key != "transcript_text"}
        evidence = {
            key: value for key, value in payload.items()
            if key not in {"article_discovery_ids", "acquisition_discovery_id"}
        }
        candidate_time = source.get("published_at")
        # The retained aggregate transcript has no per-article observation clock.
        observed = None
    elif strategy == "supply_chain":
        ticker = source["symbol"]
        published = _published_at(source)
        native = source.get("id") or source.get("article_id") or source.get("url")
        if not native:
            if not all(source.get(key) for key in ("source", "headline")) or not published:
                raise ValueError("supply-chain source lacks a native identity")
            native = {"source": source["source"], "headline": source["headline"], "published_at": published}
        source_key = stable_id("source_event", strategy, native)
        evidence = {key: value for key, value in source.items() if key != "observed_at"}
        public_source = {
            key: source[key] for key in ("id", "article_id", "url", "source", "headline", "symbol")
            if key in source
        }
        public_source["published_at"] = published
        candidate_time = published
    else:
        ticker = source.get("issuer_attribution", {}).get("ticker", "")
        native = source.get("award_key") or source["award_id"]
        source_key = stable_id("source_event", strategy, native)
        evidence = {key: value for key, value in source.items() if key != "observed_at"}
        fields = (
            "award_key", "award_id", "base_obligation_date", "amount", "amount_basis",
            "award_scope", "last_modified_date", "recipient_uei", "parent_recipient_uei",
            "recipient_identity_status",
        )
        public_source = {key: source[key] for key in fields if key in source}
        public_source["attribution_status"] = source.get("issuer_attribution", {}).get("status")
        candidate_time = observed
    if not isinstance(ticker, str):
        raise ValueError("invalid source ticker identity")
    for label, value in (("candidate availability", candidate_time), ("source observed_at", observed)):
        if value is not None:
            aware(value, label)
    return _event_key(strategy, ticker, native), {
        "_source_ticker": ticker,
        "source_event_key": source_key,
        "evidence_sha256": digest(evidence),
        "source_identity": native,
        "source": public_source,
        "candidate_availability_at": candidate_time,
        "source_observed_at": observed,
    }


def _check_identity(document: dict, source_identity: dict, purpose: str) -> None:
    identity = document["identity"]
    for key in ("generation", "session", "commit", "configuration"):
        if identity.get(key) != source_identity.get(key):
            raise ValueError(f"{purpose} identity mismatch: {key}")
    if identity.get("purpose") != purpose:
        raise ValueError(f"unsupported {purpose} identity purpose")


def _check_bindings(source: dict, screen: dict, decision: dict) -> None:
    identity = source["identity"]
    _check_identity(screen, identity, "research-screening-v1")
    _check_identity(decision, identity, "prospective-decision-inputs-v1")
    binding = screen["payload"]["binding"]
    receipt = screen["payload"]["receipt"]
    context = decision["payload"]["context"]
    if binding.get("source_identity") != identity:
        raise ValueError("screening source identity mismatch")
    references = (binding, receipt, context, decision["payload"]["research_binding"])
    if any(value.get("source_digest") != source["digest"] for value in references):
        raise ValueError("source digest binding mismatch")
    epoch = binding.get("epoch_id")
    if (not isinstance(epoch, str) or not epoch
            or decision["payload"]["research_binding"].get("epoch_id") != epoch):
        raise ValueError("decision research epoch identity mismatch or missing")
    if (receipt.get("session") != identity["session"]
            or context.get("reference_session") != identity["session"]
            or receipt.get("epoch_id") != binding.get("epoch_id")
            or screen["identity"].get("epoch_id") != binding.get("epoch_id")):
        raise ValueError("screening/decision session or epoch identity mismatch")
    cutoff = aware(context["cutoff"], "decision cutoff")
    for document, field in ((source, "source availability"), (screen, "screening availability")):
        if aware(document["acquired_at"], field) > cutoff:
            raise ValueError(f"{field} is after decision cutoff")


def _rank(strategy: str, row: dict) -> list:
    score = row.get("score")
    valid = type(score) in (int, float) and math.isfinite(score)
    if not valid:
        if row.get("reason") != "invalid_score" or "invalid_score_value" not in row:
            raise ValueError("unsupported invalid score representation")
        return [1, row["ticker"], row["discovery_id"], row["invalid_score_value"]]
    rank = [0]
    if strategy == "govt_contracts":
        rank.append(row["journal_only"])
    return rank + [-float(score), row["ticker"], row["discovery_id"]]


def _validate_manifest(manifest: dict, strategy: str) -> None:
    policy = (
        "verified_issuer_first_score_desc_ticker_source_identity_v1"
        if strategy == "govt_contracts" else "score_desc_ticker_source_identity_v1"
    )
    if (manifest.get("version") != 1 or manifest.get("strategy") != strategy
            or manifest.get("policy") != policy
            or manifest.get("population") != "screen_qualifying_hypotheses"
            or type(manifest.get("budget")) is not int or manifest["budget"] < 0):
        raise ValueError("unsupported admission manifest schema or policy")
    discovered = manifest["discovered"]
    by_id = {row["discovery_id"]: row for row in discovered}
    partition = manifest["admitted"] + manifest["excluded"]
    if (len(by_id) != len(discovered) or len(partition) != len(discovered)
            or len({row["discovery_id"] for row in partition}) != len(partition)
            or any(by_id.get(row["discovery_id"]) != row for row in partition)):
        raise ValueError("incomplete or inconsistent original discovery partition")
    ordered = sorted(discovered, key=lambda row: _rank(strategy, row))
    if discovered != ordered:
        raise ValueError("original rank order mismatch")
    admitted = []
    for row in ordered:
        discovery_payload = json.dumps([strategy, row["event_key"], row["direction"]], separators=(",", ":"))
        expected_id = "discovery_" + hashlib.sha256(discovery_payload.encode()).hexdigest()[:24]
        if row["discovery_id"] != expected_id or type(row["journal_only"]) is not bool:
            raise ValueError("candidate discovery identity mismatch")
        universe = row.get("universe", {})
        if universe.get("symbol", row["ticker"]) != row["ticker"]:
            raise ValueError("candidate universe ticker identity mismatch")
        reason = "admitted" if len(admitted) < manifest["budget"] else "analysis_budget"
        if universe.get("decision") in UNIVERSE_EXCLUSIONS:
            reason = "equity_universe:" + universe["decision"]
        if row.get("reason") == "invalid_score":
            reason = "invalid_score"
        if row["reason"] != reason:
            raise ValueError("original admission reason does not replay")
        if reason == "admitted":
            admitted.append(row)
    if admitted != manifest["admitted"]:
        raise ValueError("original admitted identities do not replay")


def _project_rows(manifest: dict, strategy: str, lookup: dict, source: dict, cutoff: str) -> list:
    _validate_manifest(manifest, strategy)
    rows = []
    for rank, original in enumerate(manifest["discovered"], 1):
        entries = lookup.get(original["event_key"], [])
        if not entries:
            raise ValueError("candidate event/ticker identity lacks a matching retained source")
        if any(entry["_source_ticker"] != original["ticker"] for entry in entries):
            raise ValueError("candidate ticker differs from authoritative source issuer")
        if len({entry["evidence_sha256"] for entry in entries}) != 1:
            raise ValueError("ambiguous same-event retained source content")
        row = dict(entries[0])
        row.pop("_source_ticker")
        observed_times = [entry["source_observed_at"] for entry in entries
                          if entry["source_observed_at"] is not None]
        if observed_times:
            row["source_observed_at"] = max(
                observed_times, key=lambda value: aware(value, "source observed_at")
            )
        row.pop("source_array_index")
        row["source_array_indices"] = [entry["source_array_index"] for entry in entries]
        row.update(original)
        row["original_rank"] = rank
        row["original_rank_key"] = _rank(strategy, original)
        row["relevance"] = {"held": "unknown", "pending": "unknown"}
        row["availability"] = {
            "candidate_at": row.pop("candidate_availability_at"),
            "source_observed_at": row.pop("source_observed_at"),
            "envelope_acquired_at": source["acquired_at"],
        }
        known = [value for value in row["availability"].values() if value is not None]
        row["available_at"] = max(known, key=lambda value: aware(value, "evidence availability"))
        if aware(row["available_at"], "evidence availability") > aware(cutoff, "decision cutoff"):
            raise ValueError("candidate evidence availability is after decision cutoff")
        rows.append(row)
    return rows


def _validate_acquisition(manifest: dict) -> None:
    discovered = manifest["discovered"]
    partition = manifest["admitted"] + manifest["excluded"]
    if (manifest["discovered_count"] != len(discovered)
            or manifest["admitted_count"] != len(manifest["admitted"])
            or manifest["excluded_count"] != len(manifest["excluded"])
            or len(discovered) != len(partition)):
        raise ValueError("incomplete acquisition population")
    by_id = {row["discovery_id"]: row for row in discovered}
    if (len(by_id) != len(discovered)
            or len({row["discovery_id"] for row in partition}) != len(partition)
            or any(by_id.get(row["discovery_id"]) != row for row in partition)):
        raise ValueError("inconsistent acquisition discovery partition")


def export_fixtures(
    repository_root: str | Path,
    generation: str,
    expected_commit: str,
    output_dir: str | Path,
    *,
    sessions: list[str] | None = None,
) -> tuple[Path, Path]:
    """Validate everything before creating output, then write two deterministic files."""
    if expected_commit != SUPPORTED_COMMIT or generation != "gen_025":
        raise ValueError("unsupported revision/generation: this exporter supports gen_025 at e33cc9e only")
    root = Path(repository_root).resolve()
    output = Path(output_dir).resolve()
    if output.is_relative_to(root):
        raise ValueError("output must be outside the source repository root")
    names = ("selection-fixture.json", "acquisition-evidence.json")
    for name in names:
        if (output / name).is_symlink():
            raise ValueError("output files must not be symbolic links")
    requested = set(sessions) if sessions is not None else None
    if requested is not None:
        for session in requested:
            if date.fromisoformat(session).isoformat() != session:
                raise ValueError("invalid requested session")
    directory = root / "data/generations" / generation / "source_inputs"
    selection = {
        "schema_version": 1, "generation": generation, "code_commit": expected_commit,
        "provenance": [], "cases": [], "notes": list(NOTES),
        "evidence_hash_policy": dict(EVIDENCE_HASH_POLICY),
    }
    acquisition = {
        "schema_version": 1, "generation": generation, "code_commit": expected_commit,
        "provenance": selection["provenance"], "acquisition": [],
    }
    seen = set()
    for path in sorted(directory.glob("*.json")):
        source, source_provenance = read_envelope(path)
        identity = source["identity"]
        session = identity.get("session")
        if requested is not None and session not in requested:
            continue
        if (identity.get("generation") != generation or identity.get("commit") != expected_commit
                or not isinstance(identity.get("configuration"), str)):
            raise ValueError("source identity mismatch")
        if date.fromisoformat(session).isoformat() != session or session in seen:
            raise ValueError("invalid or duplicate source session identity")
        seen.add(session)
        screen, screen_provenance = read_envelope(directory / "screening" / path.name)
        decision, decision_provenance = read_envelope(directory / "decision_inputs" / path.name)
        _check_bindings(source, screen, decision)
        for provenance in (source_provenance, screen_provenance, decision_provenance):
            provenance["path"] = str(Path(provenance["path"]).relative_to(root))
            selection["provenance"].append(provenance)
        payload = source["payload"]
        finnhub = payload["finnhub"]
        sources = {
            "earnings_call": finnhub["transcripts"],
            "supply_chain": finnhub["disruption_news"],
            "govt_contracts": payload["usaspending"]["data"]["contracts"],
        }
        lookups = {strategy: defaultdict(list) for strategy in STRATEGIES}
        for strategy, objects in sources.items():
            for index, source_row in enumerate(objects):
                # Nonqualifying raw source rows can lack complete event identity.
                # They are not invented into the qualifying discovery population.
                try:
                    event_key, entry = project_source(strategy, source_row)
                except (KeyError, TypeError):
                    continue
                entry["source_array_index"] = index
                lookups[strategy][event_key].append(entry)
        admitted_acquisition = finnhub["coverage"]["acquisition_admission"]
        for manifest in [admitted_acquisition["earnings_news"], *admitted_acquisition["earnings_articles"]]:
            _validate_acquisition(manifest)
        acquisition["acquisition"].append({
            "session": session,
            "request_window": finnhub["coverage"]["request_window"],
            "supply_chain_query_universe": finnhub["coverage"]["supply_chain_query_universe"],
            "earnings_calendar": admitted_acquisition["earnings_news"],
            "earnings_articles": admitted_acquisition["earnings_articles"],
            "raw_disruption_news_count": len(sources["supply_chain"]),
            "raw_contract_count": len(sources["govt_contracts"]),
        })
        horizons = screen["payload"]["horizons"]
        if set(horizons) != HORIZONS:
            raise ValueError("unsupported or incomplete screening horizons")
        context = decision["payload"]["context"]
        for horizon, horizon_data in horizons.items():
            health_rows = [row for row in horizon_data["health"] if row["strategy"] in STRATEGIES]
            if len(health_rows) != 3 or {row["strategy"] for row in health_rows} != set(STRATEGIES):
                raise ValueError("incomplete strategy screening population")
            for health in health_rows:
                if health.get("epoch_id") != screen["payload"]["binding"]["epoch_id"]:
                    raise ValueError("selected strategy health epoch identity mismatch")
                strategy = health["strategy"]
                manifest = health["evidence"]["admission_manifest"]
                rows = _project_rows(manifest, strategy, lookups[strategy], source, context["cutoff"])
                selection["cases"].append({
                    "session": session, "horizon": horizon, "strategy": strategy,
                    "budget": manifest["budget"], "population": manifest["population"],
                    "original_policy": manifest["policy"], "rows": rows,
                    "expected_admitted_ids": [row["discovery_id"] for row in manifest["admitted"]],
                    "source_observed_at": source["acquired_at"],
                    "screen_observed_at": screen["acquired_at"],
                    "decision_context": context,
                    "source_acquisition": payload["_decision_acquisition"],
                    "cutoff_at": context["cutoff"],
                })
    if not seen or (requested is not None and seen != requested):
        raise ValueError("requested source session is unavailable")
    # Original source ordering is deterministic by envelope filename, and each
    # JSON object is serialized with sorted keys; no capture-time fields are added.
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, document in zip(names, (selection, acquisition)):
        target = output / name
        with tempfile.NamedTemporaryFile("w", dir=output, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        temporary.replace(target)
        paths.append(target)
    return paths[0], paths[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    try:
        paths = export_fixtures(args.repository_root, args.generation, args.expected_commit,
                                args.output_dir, sessions=args.sessions)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, f"Export rejected: {error}\n")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
