"""Material images cannot enter research without their exact source binding."""
from copy import deepcopy
import hashlib
import importlib

import pytest


def api():
    return importlib.import_module('tradingagents.strategies.data_sources.regulation_graphics_inputs')


def test_legacy_text_evidence_has_no_invented_graphic_dependency():
    from test_regulation_evidence import build
    assert api().graphics_assets(build()) == []


def test_unbound_graphics_cannot_be_introduced_by_caller():
    from test_regulation_evidence import build
    evidence = deepcopy(build())
    evidence['units'][0]['government_copy'] = {'graphics': [{'graphic_id': 'EP05OC26.001'}]}
    with pytest.raises(ValueError):
        api().graphics_assets(evidence)


def test_normalization_preserves_source_image_bytes_and_exact_context(monkeypatch):
    from tradingagents.strategies.data_sources import regulation_evidence
    raw = b'\x89PNG\r\n\x1a\nretained-test-boundary'
    receipt = regulation_evidence.original_receipt(raw, observed_at='2026-10-15T22:00:00+00:00',
        url='https://img.federalregister.gov/EP05OC26.001/EP05OC26.001_original_size.png')
    evidence = {'units': [{'representation': 'verified_government_copy_with_graphics',
        'government_copy': {'graphics': [{'graphic_id': 'EP05OC26.001', 'marker_id': 'TP05OC26.001',
            'context_before': 'Annual cost ($ thousands)\n', 'context_after': '\nFootnote 1: per firm.',
            'receipt': receipt}]}}]}
    # The source module independently validates full PNG/FR identity. This
    # boundary test isolates normalization after that mandatory validation.
    calls = []
    monkeypatch.setattr(regulation_evidence, 'validate_regulation_evidence', lambda value: calls.append(value))
    assets = api().graphics_assets(evidence)
    assert calls == [evidence]
    assert assets == [{'id': 'EP05OC26.001', 'filename': 'EP05OC26.001.png',
        'sha256': hashlib.sha256(raw).hexdigest(), 'data': raw,
        'context': 'Annual cost ($ thousands)\n\n[graphic TP05OC26.001]\n\nFootnote 1: per firm.'}]
    evidence['units'].append(deepcopy(evidence['units'][0]))
    with pytest.raises(ValueError, match='invalid_regulation_graphics'):
        api().graphics_assets(evidence)
