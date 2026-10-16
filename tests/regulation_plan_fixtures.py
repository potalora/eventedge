"""Inert plan identity for constructed workflows, never a credential store."""
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def inert_plan_identity(monkeypatch, tmp_path):
    # Keep real configured_plan_client and require_configured_billing checks.
    # Only their private identity-reader boundary is constructed. Explicitly
    # select an absent temporary path, preventing dependence on developer auth.
    auth_path = tmp_path / 'empty-plan-auth' / 'plan-auth.json'
    reads = []
    module = ModuleType('tradingagents.strategies.plan_auth')
    class PlanAuthError(RuntimeError):
        pass
    module.PlanAuthError = PlanAuthError
    def identity(path, *, deadline):
        assert path == auth_path
        assert not auth_path.exists()
        reads.append(path)
        return 'constructed-regulation-workflow-profile'
    def forbidden_credentials(*args, **kwargs):
        pytest.fail('constructed workflow attempted credential resolution')
    native = sys.modules.get(module.__name__)
    if native is not None:
        for name in ('read_profile_identity', 'get_plan_credentials', '_store'):
            if hasattr(native, name):
                monkeypatch.setattr(native, name, forbidden_credentials)
    module.read_profile_identity = identity
    module.get_plan_credentials = forbidden_credentials
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return SimpleNamespace(auth_path=str(auth_path), reads=reads)
