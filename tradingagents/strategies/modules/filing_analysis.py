from __future__ import annotations

import logging
from typing import Any

from .base import Candidate
from .admission import admit_candidates
from ..data_sources.edgar_source import normalize_filing_form, filing_form_family

logger = logging.getLogger(__name__)

class FilingAnalysisStrategy:
    """Analyze SEC filing contents for material events and compensation changes.

    Processes all EDGAR filing types in a single pass:
    - 10-K/10-Q: material changes analysis (from P3)
    - DEF 14A: executive compensation analysis (from P9)
    - 8-K/SC 13D/SC 13G: source-text analysis; filing occurrence alone is not a thesis.

    Academic basis:
    - Cohen et al. (2020, JoF "Lazy Prices"): 10-K/10-Q language changes
      predict 3.5-4.5%/year underperformance.
    - Core et al. (1999, JFE), Bebchuk & Fried (2004): compensation design
      linked to firm value.
    """

    name = "filing_analysis"
    track = "paper_trade"
    data_sources = ["edgar", "yfinance", "openbb"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "forms_to_analyze": (
                ["10-K", "10-Q"],
                ["10-K", "10-Q", "DEF 14A", "8-K", "SCHEDULE 13D", "SCHEDULE 13G"],
            ),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "forms_to_analyze": ["10-K", "10-Q", "DEF 14A", "8-K", "SCHEDULE 13D", "SCHEDULE 13G"],
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen EDGAR filings for material changes, exec comp shifts and material events."""
        edgar_data = data.get("edgar", {})
        filings = [*edgar_data.get("filings", []), *edgar_data.get("activist_13d", []), *edgar_data.get("passive_13g", [])]

        if not filings:
            return []

        forms_to_analyze = params.get("forms_to_analyze", ["10-K", "10-Q", "DEF 14A"])
        forms_to_analyze = {filing_form_family(form) for form in forms_to_analyze}
        candidates = []

        for filing in filings:
            form_type = normalize_filing_form(filing.get("form_type", ""))
            form_family = filing_form_family(form_type)
            entity_name = filing.get("entity_name", "")
            ticker = filing.get("ticker", "")
            filing_identity = {
                "accession_number": filing.get("accession_number")
                or filing.get("adsh"),
                "file_url": filing.get("file_url", ""),
            }

            # 10-K / 10-Q → material changes analysis (from P3)
            if form_type in ("10-K", "10-Q") and form_type in forms_to_analyze:
                current_text = filing.get("current_text", "")
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM analyzer will determine direction
                        score=0.5,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": current_text,
                            "prior_text": filing.get("prior_text", ""),
                            "needs_llm_analysis": True,
                            "analysis_type": "filing_change",
                            **filing_identity,
                        },
                    )
                )

            # DEF 14A → exec comp analysis (from P9)
            elif form_type == "DEF 14A" and form_type in forms_to_analyze:
                proxy_text = filing.get("proxy_text", "")
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM analyzer will determine direction
                        score=0.5,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "proxy_text": proxy_text,
                            "needs_llm_analysis": True,
                            "analysis_type": "exec_comp",
                            **filing_identity,
                        },
                    )
                )

            # 8-K → material event announcement
            elif form_type == "8-K" and form_type in forms_to_analyze:
                event_text = filing.get("current_text", "")
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM will determine
                        score=0.6,  # 8-Ks are time-sensitive
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": event_text[:5000],
                            "needs_llm_analysis": True,
                            "analysis_type": "material_event",
                            **filing_identity,
                        },
                    )
                )

            # SC 13D/13G → activist or large passive stake
            elif form_family in ("SCHEDULE 13D", "SCHEDULE 13G") and form_family in forms_to_analyze:
                stake_text = filing.get("current_text", "")
                is_activist = form_family == "SCHEDULE 13D"
                # Schedule 13 reporters may differ from the subject issuer.
                # Generic EDGAR display-name tickers are not subject attribution.
                subject_ticker = filing.get("subject_ticker")
                candidates.append(
                    Candidate(
                        ticker=subject_ticker or ticker,
                        journal_only=not bool(subject_ticker),
                        date=date,
                        direction="long",  # Activist stakes are typically bullish
                        score=0.7 if is_activist else 0.4,
                        metadata={
                            "form_type": form_type,
                            "source_form_type": filing.get("source_form_type", filing.get("form_type", "")),
                            "subject_attribution_verified": bool(subject_ticker),
                            **({"non_actionable_reason": "unverified_subject_issuer"} if not subject_ticker else {}),
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": stake_text[:5000],
                            "needs_llm_analysis": True,
                            "analysis_type": "activist_stake"
                            if is_activist
                            else "passive_stake",
                            **filing_identity,
                        },
                    )
                )

        # Preserve distinct source-native events until the committee decision view.
        unique = []
        seen = set()
        for candidate in candidates:
            identity = (candidate.ticker, candidate.metadata.get("accession_number") or candidate.metadata.get("file_url"), candidate.metadata["analysis_type"])
            if identity in seen:
                continue
            seen.add(identity)
            text = candidate.metadata.get("current_text") or candidate.metadata.get("proxy_text")
            if not text:
                candidate.journal_only = True
                candidate.metadata["non_actionable_reason"] = "missing_source_text"
            unique.append(candidate)

        # Enrich with analyst consensus for contradiction detection
        openbb_data = data.get("openbb", {})
        estimates = openbb_data.get("estimates", {})
        profile_data = openbb_data.get("profile", {})
        for candidate in unique:
            ticker = candidate.ticker
            if isinstance(estimates, dict) and ticker in estimates:
                est = estimates[ticker]
                candidate.metadata["consensus_eps"] = est.get("consensus_eps")
                candidate.metadata["consensus_revenue"] = est.get("consensus_revenue")
                candidate.metadata["price_target_mean"] = est.get("price_target_mean")
                if est.get("num_analysts", 0) >= 5:
                    candidate.score = min(candidate.score * 1.1, 1.0)
            if isinstance(profile_data, dict) and ticker in profile_data:
                candidate.metadata["sector"] = profile_data[ticker].get("sector", "")

        return admit_candidates(self.name, unique, params.get("analysis_budget"))

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
        """Exit on hold period."""
        hold_days = params.get("hold_days", 25)
        if holding_days >= hold_days:
            return True, "hold_period"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a unified Filing Analysis strategy that processes
EDGAR filings: 10-K/10-Q (material changes), DEF 14A (exec comp),
8-K (material events), SC 13D (activist stakes), SC 13G (large passive stakes).

Investment horizon: 30 days. Filing implications unfold over weeks as
analysts digest. Filing occurrence does not by itself establish a directional thesis.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- forms_to_analyze: subset of ["10-K", "10-Q", "DEF 14A", "8-K", "SCHEDULE 13D", "SCHEDULE 13G"]

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
