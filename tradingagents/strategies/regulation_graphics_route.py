"""Local admission for the finite regulatory-originals Sol exception."""
from __future__ import annotations

from .plan_document_inputs import ORIGINALS_LABEL_POLICY, document_input_metadata


def validate_regulation_graphics_route(*, model_policy, model, effort, document_inputs,
                                       max_tokens, temperature, max_call_seconds):
    """Validate only received transport fields; profile and role remain upstream.

    Logical limits are local guards, never options for the subscription SDK.
    Ordered metadata validation independently verifies original bytes and hashes.
    """
    if (type(model_policy) is not str or model_policy != 'sol_high_v1'
            or type(model) is not str or model != 'gpt-6.1-sol'
            or type(effort) is not str or effort != 'high'
            or type(max_tokens) is not int or max_tokens != 4096
            or type(temperature) not in (int, float) or temperature != 0.0
            or type(max_call_seconds) not in (int, float) or max_call_seconds != 240):
        raise ValueError('invalid_regulation_graphics_model_policy')
    documents = document_input_metadata(document_inputs)
    if any(item['media_type'] != 'image/png'
           or item.get('presentation_policy') != ORIGINALS_LABEL_POLICY for item in documents):
        raise ValueError('invalid_regulation_graphics_model_policy')
