"""Held admission until explicitly retained claims pass scoped analysis."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .admission import admit_candidates
from .base import Candidate


class BoundedDocumentResearchStrategy:
    name = 'bounded_document_research'
    track = 'paper_trade'
    data_sources = ['bounded_documents']

    def get_param_space(self, horizon='30d'):
        return {}

    def get_default_params(self, horizon='30d'):
        return {'analysis_budget': 4, 'max_positions': 4}

    def screen(self, data, date, params):
        budget = params.get('analysis_budget', params.get('max_positions', 4))
        if type(budget) is not int or not 0 <= budget <= 4:
            raise ValueError('bounded_document_analysis_budget_invalid')
        envelope = data.get('bounded_documents', {})
        if (type(envelope) is not dict or envelope.get('error')
                or envelope.get('coverage') != {'complete': True}):
            return admit_candidates(self.name, [], budget)
        from tradingagents.strategies.bounded_document_research import validate_evidence
        from tradingagents.strategies.orchestration.bounded_document_inputs import compact_source
        rows = envelope.get('bounded_documents')
        if type(rows) is not list or not 1 <= len(rows) <= 4:
            raise ValueError('bounded_document_screen_invalid')
        candidates = []
        for row in rows:
            if type(row) is not dict or set(row) not in ({'ticker', 'evidence'}, {'ticker', 'evidence', 'decision_scope'}):
                raise ValueError('bounded_document_screen_invalid')
            evidence = row['evidence']
            validate_evidence(evidence, session_as_of=date)
            identity = hashlib.sha256(json.dumps([evidence['document_id'], evidence['original']['sha256']],
                                                separators=(',', ':')).encode()).hexdigest()
            source_key = 'bounddoc_' + identity
            candidates.append(Candidate(ticker=row['ticker'], date=date, journal_only=True,
                event_key=source_key, source_event_keys=(source_key,), strategy_tags=(self.name,), score=0.5,
                metadata={'analysis_type': self.name, 'needs_llm_analysis': True,
                          'bounded_document_evidence': deepcopy(evidence),
                          'bounded_document_source': compact_source(evidence),
                          **({'bounded_document_decision_scope':deepcopy(row['decision_scope'])}
                             if 'decision_scope' in row else {}),
                          'bounded_document_discovery_id': source_key,
                          'observed_at': evidence['as_of']}))
        return admit_candidates(self.name, candidates, budget)

    def check_exit(self, ticker, entry_price, current_price, holding_days, params, data, direction='long'):
        return False, ''

    def build_propose_prompt(self, context):
        return 'Retained document research is journal-only with a fixed bounded admission budget.'
