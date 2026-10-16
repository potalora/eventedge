"""Explicit subscription selection; an inert descriptor holds no credentials."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time


@dataclass(frozen=True, repr=False)
class ChatGPTPlanClient:
    auth_path: str | None
    profile_identity: str

    def __repr__(self):
        return 'ChatGPTPlanClient(<private profile>)'

    def close(self):
        """No resources or bearer credentials exist in the parent."""


def _mode(config):
    mode = config.get('autoresearch', {}).get('llm_billing_mode', 'api')
    if mode not in ('api', 'chatgpt_plan'):
        raise ValueError('invalid_llm_billing_mode')
    return mode


def _plan_model(model):
    from .llm_utils import uses_responses
    if not uses_responses(model):
        raise ValueError('chatgpt_plan_model_unsupported')


def _profile_identity(path):
    from .plan_auth import read_profile_identity
    from .runtime_deadline import current_model_deadline
    deadline = current_model_deadline()
    return read_profile_identity(Path(path) if path else None,
                                 deadline=min(time.monotonic()+2, deadline if deadline is not None else float('inf')))


def _selected_path(config):
    path = config.get('autoresearch', {}).get('chatgpt_plan_auth_path')
    if path is not None and (not isinstance(path, str) or not path):
        raise ValueError('invalid_chatgpt_plan_auth_path')
    return path


def configured_plan_client(config, model):
    """Return None only for explicit/default API mode; never silently fall back."""
    if _mode(config) == 'api':
        return None
    _plan_model(model)
    path = _selected_path(config)
    identity = _profile_identity(path)
    return ChatGPTPlanClient(path, identity)


def require_configured_billing(client, config, model):
    """Reject injected paid clients and a profile replaced during a run."""
    plan = isinstance(client, ChatGPTPlanClient)
    if (_mode(config) == 'chatgpt_plan') != plan:
        raise ValueError('llm_billing_mode_mismatch')
    if plan:
        _plan_model(model)
        if client.auth_path != _selected_path(config):
            raise ValueError('chatgpt_plan_profile_changed')
        identity = _profile_identity(client.auth_path)
        if identity != client.profile_identity:
            raise ValueError('chatgpt_plan_profile_changed')
