"""The deployed finite window cannot silently turn into continuing authority.

The recognition/review is constructed test evidence. Only the effective window
comes from the retained deployment profile; no claim about native OCR is made.
"""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from test_document_decision_eligibility import fixture
from tradingagents.strategies.document_decision_eligibility import assess_decision_eligibility


pytestmark = pytest.mark.scheduled_acceptance
PROFILE = Path(__file__).parent / "fixtures/scheduled_acceptance/production-profile.json"


def scoped_evidence():
    recognition, scope = fixture()
    retained = json.loads(PROFILE.read_text())["autoresearch"]["bounded_document_registry"][0]["decision_scope"]
    scope.update({key: retained[key] for key in ("effective_from", "effective_until")})
    return recognition, scope


@pytest.mark.parametrize("instant", [
    "2026-10-14T22:00:00+00:00",
    "2026-10-17T03:59:58.999999+00:00",
    "2026-10-17T03:59:59+00:00",
])
def test_finite_scope_accepts_supported_instant_through_exact_inclusive_end(instant):
    recognition, scope = scoped_evidence()
    before = deepcopy(scope)
    result = assess_decision_eligibility(recognition, scope, ticker="ACME", session_as_of=instant)
    assert result["eligible"] is True
    assert result["production_authority"] is False
    assert result["execution_authority"] == "ordinary_committee_and_risk_gates"
    assert scope == before


@pytest.mark.parametrize("instant", [
    "2026-10-17T03:59:59.000001+00:00",
    "2026-10-19T22:00:00+00:00",
    "2026-10-19",
    # Date-only evidence is conservatively evaluated at New York time.max.
    # The deployed whole-second end does not authorize the remaining fraction.
    "2026-10-16",
])
def test_expiry_refuses_authority_and_cannot_renew_retained_scope(instant):
    recognition, scope = scoped_evidence()
    before = deepcopy(scope)
    with pytest.raises(ValueError, match="^invalid_document_decision_window$"):
        assess_decision_eligibility(recognition, scope, ticker="ACME", session_as_of=instant)
    assert scope == before


def test_last_fully_covered_date_retains_ordinary_gates():
    recognition, scope = scoped_evidence()
    result = assess_decision_eligibility(recognition, scope, ticker="ACME", session_as_of="2026-10-15")
    assert result["eligible"] is True
    assert result["production_authority"] is False
