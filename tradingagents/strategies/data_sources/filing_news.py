"""Pure source identity/content validation shared by full-filing consumers."""
from __future__ import annotations

import hashlib
import json


def immutable_source_locator(record, fields):
    """Select an existing identity without coercing bools or containers."""
    if not isinstance(record, dict):
        return None
    for field in fields:
        value = record.get(field)
        if type(value) is int or (type(value) is str and value.strip()):
            return str(value)
    return None


def prepare_filing_news(records, expected_refs=None):
    """Validate every raw row before retaining one genuine observation per ID."""
    selected, contents = {}, {}
    for article in sorted(records, key=lambda value: json.dumps(value, sort_keys=True, default=str)):
        locator = immutable_source_locator(article, ('article_id', 'id', 'url'))
        if locator is None:
            raise ValueError('invalid_filing_source')
        ref = 'FINNHUB:' + locator
        headline, summary = article.get('headline', ''), article.get('summary', '')
        if type(headline) is not str or type(summary) is not str:
            raise ValueError('invalid_filing_source')
        text = headline + ' ' + summary
        value = {'source': 'FINNHUB', 'source_id': locator, 'text': text,
                 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                 'observed_at': article.get('observed_at'), 'url': article.get('url')}
        if article.get('published_at'):
            value['published_at'] = article['published_at']
        content = (headline, summary, article.get('url'), article.get('published_at'))
        if ref in selected:
            if contents[ref] != content:
                raise ValueError('invalid_filing_source')
        else:
            selected[ref], contents[ref] = value, content
    if expected_refs is not None and sorted(selected) != expected_refs:
        raise ValueError('invalid_filing_source')
    return [selected[ref] for ref in sorted(selected)]
