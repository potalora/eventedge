"""Exact image inputs from a validated complete regulatory source corpus."""
from __future__ import annotations


def graphics_assets(evidence):
    """Rebind source originals before exposing bytes to bounded recognition.

    Raw material coverage is distinct from recognition/review and a supported
    research conclusion. No caller-provided transcript certifies these bytes.
    """
    from .regulation_evidence import validate_regulation_evidence, receipt_bytes
    if evidence.get('version') == 'regulation-publication-analysis-v1':
        from .regulation_publications import validate_analysis_view
        validate_analysis_view(evidence)
    else:
        validate_regulation_evidence(evidence)
    assets, seen = [], set()
    try:
        for unit in evidence['units']:
            graphics = unit.get('government_copy', {}).get('graphics', [])
            if graphics and unit.get('representation') != 'verified_government_copy_with_graphics':
                raise ValueError('invalid_regulation_graphics_source')
            for graphic in graphics:
                identifier = graphic['graphic_id']
                if identifier in seen or len(seen) >= 128:
                    raise ValueError('invalid_regulation_graphics_source')
                seen.add(identifier)
                receipt = graphic['receipt']
                assets.append({'id': identifier, 'filename': identifier + '.png',
                    'sha256': receipt['sha256'], 'data': receipt_bytes(receipt, 4 * 1024**2),
                    'context': graphic['context_before'] + '\n[graphic ' + graphic['marker_id']
                        + ']\n' + graphic['context_after']})
        return assets
    except (KeyError, TypeError, AttributeError):
        raise ValueError('invalid_regulation_graphics_source') from None
