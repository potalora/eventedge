"""The yearly delivery is explicit and changes source/cache identity."""
from unittest.mock import Mock

import pytest

from tradingagents.strategies.data_sources.registry import build_default_registry
from tradingagents.strategies.orchestration.source_inputs import (
    registered_source_fingerprint,
    source_configuration_fingerprint,
)


def test_yearly_policy_selects_public_adapter_without_noaa_credentials(monkeypatch):
    monkeypatch.delenv('NOAA_CDO_TOKEN', raising=False)
    source = build_default_registry({'noaa_delivery_policy': 'ghcnd_yearly_v1'}).get('noaa')
    assert type(source).__name__ == 'NOAAYearlySource'
    assert source.requires_api_key is False
    assert source.is_available() is True


def test_absent_yearly_policy_preserves_existing_adapter():
    source = build_default_registry({}).get('noaa')
    assert type(source).__name__ == 'NOAASource'


@pytest.mark.parametrize('policy', [None, '', 'access', 'ghcnd_yearly_v2', True, [], {}])
def test_unknown_explicit_policy_rejected_before_provider_construction(policy, monkeypatch):
    constructor = Mock(side_effect=AssertionError('provider construction before policy validation'))
    monkeypatch.setattr(
        'tradingagents.strategies.data_sources.yfinance_source.YFinanceSource', constructor)
    with pytest.raises(ValueError, match='invalid_noaa_delivery_policy'):
        build_default_registry({'noaa_delivery_policy': policy})
    constructor.assert_not_called()


def test_yearly_policy_and_adapter_both_bind_source_identity():
    legacy = build_default_registry({}).get('noaa')
    yearly = build_default_registry({'noaa_delivery_policy': 'ghcnd_yearly_v1'}).get('noaa')
    legacy_config = source_configuration_fingerprint({'autoresearch': {}})
    yearly_config = source_configuration_fingerprint(
        {'autoresearch': {'noaa_delivery_policy': 'ghcnd_yearly_v1'}})
    assert legacy_config != yearly_config
    assert registered_source_fingerprint(legacy_config, legacy) != registered_source_fingerprint(
        legacy_config, yearly)
