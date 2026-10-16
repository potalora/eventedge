"""Model assessments must not rewrite the source observation's identity."""
from copy import deepcopy

import pytest

from tradingagents.strategies.modules.admission import _source_identity
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.multi_strategy_engine import _canonical_signal_evidence


@pytest.mark.parametrize('field', ['document_assessment', 'filing_assessment'])
def test_filing_assessment_is_excluded_from_source_evidence_only(field):
    original = {'accession_number': '0001234567-26-000001', 'source_text': 'Actual source'}
    assessed = deepcopy(original)
    assessed[field] = {'status': 'complete', 'rationale': 'Model conclusion'}
    before = deepcopy(assessed)
    assert _canonical_signal_evidence(assessed) == _canonical_signal_evidence(original)
    assert assessed == before


@pytest.mark.parametrize('field', ['document_assessment', 'filing_assessment'])
def test_assessment_preserves_invalid_source_fallback_identity(field):
    candidate = Candidate(ticker='', direction='neutral',
                          score=0, metadata={'form_type': '8-K'}, date='2026-10-09')
    identity = _source_identity('filing_analysis', candidate)
    assert identity.startswith('unresolved_source_')
    candidate.metadata[field] = {'status': 'insufficient', 'reason': 'Missing source'}
    assert _source_identity('filing_analysis', candidate) == identity
