from __future__ import annotations

from datetime import date as calendar_date, datetime, timedelta
from functools import lru_cache
import hashlib
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, Mapping

from tradingagents.strategies.orchestration._io import load_json_strict

from .base import Candidate
from .admission import admit_candidates

logger = logging.getLogger(__name__)

_UEI_RE = re.compile(r"^[A-Z0-9]{12}$")

DEFAULT_CROSSWALK_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "govt_contracts_uei_crosswalk.json"
)
CROSSWALK_SHA256 = "05cfc4ea0171d0cfce439c45724105130e016bfd2bc3c1b356d2021edb26cbc8"


def _native_uei(value: object) -> str:
    """UEIs are twelve uppercase alphanumeric characters, with no coercion."""
    return value if isinstance(value, str) and _UEI_RE.fullmatch(value) else ""


def _canonical_legal_name(value: object) -> str:
    """Normalize exact legal entity name for deterministic lookup (never substring)."""
    if not isinstance(value, str):
        return ""
    text = value.strip().upper()
    if not text:
        return ""
    text = re.sub(r"[,]", " ", text)
    text = re.sub(r"\s+-\s+", " ", text)
    tokens = [tok.rstrip(".") for tok in text.split() if tok.rstrip(".")]
    return " ".join(tokens)


def _canonical_crosswalk_sha256(doc: Mapping[str, Any]) -> str:
    payload = json.dumps(
        doc, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=4)
def _load_crosswalk_cached(path_str: str, expected_sha256: str | None) -> dict[str, Any]:
    raw = load_json_strict(Path(path_str))
    if not isinstance(raw, dict):
        raise ValueError("govt_contracts crosswalk must be a JSON object")
    required = {
        "version",
        "verified_uei_issuers",
        "verified_uei_no_listed_target",
        "verified_legal_name_issuers",
    }
    missing = required - set(raw)
    if missing:
        raise ValueError(f"govt_contracts crosswalk missing required sections: {sorted(missing)}")
    for section in (
        "verified_uei_issuers",
        "verified_uei_no_listed_target",
        "verified_legal_name_issuers",
    ):
        if not isinstance(raw[section], dict):
            raise ValueError(f"govt_contracts crosswalk section {section!r} must be an object")
    actual_sha = _canonical_crosswalk_sha256(raw)
    if expected_sha256 is not None and actual_sha != expected_sha256:
        raise ValueError(
            f"govt_contracts crosswalk SHA-256 mismatch: expected {expected_sha256}, got {actual_sha}"
        )
    return {**raw, "_crosswalk_sha256": actual_sha}


def load_govt_contracts_crosswalk(
    path: Path | str | None = None,
    *,
    expected_sha256: str | None = CROSSWALK_SHA256,
) -> dict[str, Any]:
    """Load and hash-verify the externalized government contractor crosswalk."""
    target = Path(path) if path is not None else DEFAULT_CROSSWALK_PATH
    return _load_crosswalk_cached(str(target.resolve()), expected_sha256)


def resolve_govt_contract_issuer(
    contract: Mapping[str, Any],
    *,
    crosswalk: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve federal award issuer using hash-pinned UEI and exact legal-name crosswalk.

    1. Never trusts caller-supplied ticker or issuer_attribution fields.
    2. Native UEI matching (verified listed and verified private/no-listed-target)
       always takes precedence, including direct-vs-parent conflict detection.
    3. Exact canonical legal-name matching runs ONLY when a valid native 12-char
       recipient UEI is present, recipient lookup did not fail, and no UEI rule
       already resolved the recipient/parent. Substring matching is never used.
    """
    cw = crosswalk if crosswalk is not None else load_govt_contracts_crosswalk()
    uei_issuers = cw["verified_uei_issuers"]
    uei_no_listed = cw["verified_uei_no_listed_target"]
    legal_issuers = cw["verified_legal_name_issuers"]
    cw_version = str(cw.get("base_crosswalk_version") or cw["version"])
    cw_sha = str(cw.get("_crosswalk_sha256") or "")

    recipient = _native_uei(contract.get("recipient_uei"))
    identity_status = str(contract.get("recipient_identity_status") or "")
    parent = (
        _native_uei(contract.get("parent_recipient_uei"))
        if identity_status == "native_award_verified"
        else ""
    )

    def _reviewed_uei(uei: str) -> dict[str, Any] | None:
        if not uei:
            return None
        if uei in uei_issuers:
            return {**uei_issuers[uei], "status": "verified_listed_target"}
        if uei in uei_no_listed:
            return {
                **uei_no_listed[uei],
                "status": "verified_no_listed_target",
                "ticker": "",
                "cik": "",
            }
        return None

    direct, ultimate = _reviewed_uei(recipient), _reviewed_uei(parent)
    if direct and ultimate and (
        direct["status"] != ultimate["status"]
        or (direct["status"] == "verified_listed_target" and direct["cik"] != ultimate["cik"])
        or (
            direct["status"] == "verified_no_listed_target"
            and direct["ultimate_owner"] != ultimate["ultimate_owner"]
        )
    ):
        return {
            "verified": False,
            "resolved": False,
            "status": "unresolved",
            "reason": "conflicting_recipient_issuer",
        }

    identity = direct or ultimate
    if identity is not None:
        return {
            **identity,
            "provenance": list(identity["provenance"]),
            "verified": identity["status"] == "verified_listed_target",
            "resolved": True,
            **(
                {"reason": "verified_no_listed_target"}
                if identity["status"] == "verified_no_listed_target"
                else {}
            ),
            "matched_uei": recipient if direct else parent,
            "match_method": "native_uei",
            "relationship": "recipient" if direct else "parent",
            "crosswalk_version": cw_version,
            "crosswalk_sha256": cw_sha,
            "reviewed_on": identity["reviewed_on"],
            "temporal_scope": "prospective_current_ownership",
            "relationship_source": contract.get("recipient_identity_source", ""),
        }

    # Exact legal-name matching requires a valid native recipient UEI and a
    # non-failed native lookup so name-only / tampered rows remain unverified.
    if recipient and identity_status != "lookup_failed":
        direct_name = _canonical_legal_name(
            contract.get("recipient_name") or contract.get("recipient")
        )
        parent_name = (
            _canonical_legal_name(contract.get("parent_recipient_name"))
            if identity_status == "native_award_verified"
            else ""
        )
        direct_legal = legal_issuers.get(direct_name) if direct_name else None
        parent_legal = legal_issuers.get(parent_name) if parent_name else None
        if direct_legal and parent_legal and direct_legal["cik"] != parent_legal["cik"]:
            return {
                "verified": False,
                "resolved": False,
                "status": "unresolved",
                "reason": "conflicting_recipient_issuer",
            }
        legal_match = direct_legal or parent_legal
        if legal_match is not None:
            return {
                **legal_match,
                "status": "verified_listed_target",
                "provenance": list(legal_match["provenance"]),
                "verified": True,
                "resolved": True,
                "matched_uei": "",
                "matched_legal_name": direct_name if direct_legal else parent_name,
                "match_method": "verified_legal_name_exact",
                "relationship": "recipient" if direct_legal else "parent",
                "crosswalk_version": str(cw["version"]),
                "crosswalk_sha256": cw_sha,
                "reviewed_on": legal_match["reviewed_on"],
                "temporal_scope": "prospective_current_ownership",
                "relationship_source": contract.get("recipient_identity_source", ""),
            }

    return {
        "verified": False,
        "resolved": False,
        "status": "unresolved",
        "reason": "unverified_recipient_issuer",
    }


class GovtContractsStrategy:
    """Buy small/mid-cap stocks winning large federal contracts.

    Academic basis: "Trading on Government Contracts" (2025, Economics Letters)
    shows positive cumulative returns following large contract announcements.
    TenderAlpha/FactSet find 5.4-7.1% annual alpha from the "Unexpected
    Government Receivables" signal. USAspending.gov publishes awards with a
    4-6 day lag, giving retail investors a window.

    Signal logic:
    1. Screen new awards by base obligation date, using cumulative obligations at acquisition.
    2. Verify native recipient/parent UEI (or exact SEC legal name for awards with
       native UEI) against hash-pinned crosswalk evidence in
       ``config/govt_contracts_uei_crosswalk.json``.
    3. Filter by contract materiality (amount >= $10M).
    4. Go long, hold 30-60 days for market to price in the revenue impact.
    """

    name = "govt_contracts"
    track = "paper_trade"
    data_sources = ["yfinance", "usaspending"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "stop_loss_pct": (0.05, 0.15),
            "profit_target_pct": (0.05, 0.25),
            "max_positions": (2, 5),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "stop_loss_pct": 0.08,
            "profit_target_pct": 0.15,
            "max_positions": 3,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for government contractor opportunities.

        Requires a source-native USASpending award; no momentum substitute.
        """
        candidates = []

        # Try USASpending contract data first
        usaspending = data.get("usaspending", {})
        contracts = usaspending.get("data", {}).get("contracts", [])
        scope = None
        if usaspending.get("award_attribution_policy") is not None:
            from ..data_sources.award_attribution_policy import validate_attribution_scope
            scope = validate_attribution_scope(usaspending, session=date)

        crosswalk = load_govt_contracts_crosswalk()

        if contracts:
            for contract in contracts:
                recipient = (
                    contract.get("recipient_name", "")
                    or contract.get("recipient", "")
                    or ""
                ).lower()
                amount = contract.get("amount", 0) or 0
                award_id = contract.get("award_id", "")
                base_date = contract.get("base_obligation_date", "")
                # Only the adapter's declared new-award contract supports this
                # thesis. An old award modification is not a fresh contract win.
                try:
                    recent = (
                        calendar_date.fromisoformat(date)
                        - timedelta(days=params.get("award_days_back", 30))
                    ).isoformat()
                    observed = datetime.fromisoformat(contract.get("observed_at", ""))
                    valid = (
                        contract.get("award_scope") == "new_awards_only"
                        and contract.get("amount_basis") == "cumulative_award_obligations"
                        and bool(contract.get("award_key"))
                        and recent <= calendar_date.fromisoformat(base_date).isoformat() <= date
                        and observed.tzinfo is not None and observed.utcoffset() is not None
                        and not isinstance(amount, bool) and math.isfinite(amount)
                    )
                except (ValueError, TypeError):
                    valid = False
                if not valid:
                    continue

                attribution = resolve_govt_contract_issuer(contract, crosswalk=crosswalk)
                ticker = attribution.get("ticker", "")
                if not award_id or amount < 10_000_000:  # $10M minimum
                    continue

                score = min(amount / 1_000_000_000, 1.0)  # Observed cumulative obligations on the new award.
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",
                        score=score,
                        raw_signal_score=score,
                        journal_only=not attribution["verified"],
                        metadata={
                            "contractor": recipient,
                            "contract_amount": amount,
                            "source": "usaspending",
                            "award_id": award_id,
                            "award_key": contract["award_key"],
                            "base_obligation_date": base_date,
                            "amount_basis": contract["amount_basis"],
                            "award_scope": contract["award_scope"],
                            "observed_at": contract["observed_at"],
                            "recipient_uei": contract.get("recipient_uei", ""),
                            "recipient_id": contract.get("recipient_id", ""),
                            "parent_recipient_uei": contract.get("parent_recipient_uei", ""),
                            "recipient_identity_status": contract.get(
                                "recipient_identity_status", "missing_native_recipient"
                            ),
                            "recipient_identity_source": contract.get(
                                "recipient_identity_source", ""
                            ),
                            "issuer_attribution": attribution,
                            **(
                                {
                                    "award_attribution_policy": scope["policy"],
                                    "award_attribution_scope_sha256": scope["scope_sha256"],
                                    **(
                                        {"attribution_coverage_gap": "unresolved_recipient_issuer"}
                                        if attribution["status"] == "unresolved"
                                        else {}
                                    ),
                                }
                                if scope is not None
                                else {}
                            ),
                            **(
                                {"non_actionable_reason": attribution["reason"]}
                                if not attribution["verified"]
                                else {}
                            ),
                            "thesis": "Newly originated award; amount is cumulative obligations observed at acquisition, not initial obligation or incremental modification.",
                        },
                    )
                )

        # Unknown issuers stay in discovery and may use remaining journal slots,
        # but must not crowd verified issuers out of the bounded admission set.
        population = admit_candidates(
            self.name,
            candidates,
            params.get("analysis_budget", params.get("max_positions", 3)),
            rank_key=lambda candidate: (candidate.journal_only, -float(candidate.score), candidate.ticker),
            policy="verified_issuer_first_score_desc_ticker_source_identity_v1",
        )
        if scope is not None:
            from copy import deepcopy
            population.admission_manifest["award_attribution_scope"] = deepcopy(scope)
        return population

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
        direction: str = "long",
    ) -> tuple[bool, str]:
        """Exit on hold period, profit target, or stop loss."""
        hold_days = params.get("hold_days", 30)
        stop_loss = params.get("stop_loss_pct", 0.08)
        profit_target = params.get("profit_target_pct", 0.15)

        if entry_price > 0:
            pnl_pct = (1 if direction == "long" else -1) * (current_price - entry_price) / entry_price
            if pnl_pct >= profit_target:
                return True, "profit_target"
            if pnl_pct <= -stop_loss:
                return True, "stop_loss"

        if holding_days >= hold_days:
            return True, "hold_period"

        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        results = context.get("recent_results", [])

        results_text = ""
        if results:
            for r in results[-5:]:
                results_text += (
                    f"  params={r.get('params', {})}, "
                    f"sharpe={r.get('sharpe', 0):.2f}, "
                    f"return={r.get('total_return', 0):.2%}, "
                    f"trades={r.get('num_trades', 0)}\n"
                )

        return f"""You are optimizing a Government Contract Awards strategy that buys
defense/government contractor stocks after large contract announcements.

Investment horizon: 30 days. Government contract repricing takes 30-60 days
for mid/small-caps. The market underreacts to award announcements.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (holding period, target ~30 days)
- stop_loss_pct: 0.05-0.15 (stop loss percentage)
- profit_target_pct: 0.05-0.25 (take profit percentage)
- max_positions: 2-5 (max concurrent positions)

Recent backtest results:
{results_text or "  No results yet."}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""
