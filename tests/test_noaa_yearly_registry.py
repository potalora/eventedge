"""NOAA registry wiring tests."""
from unittest.mock import Mock

import pytest

from tradingagents.strategies.data_sources.registry import build_default_registry
from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint


def test_registry_registers_public_station_adapter_without_noaa_credentials(monkeypatch):
    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    source = build_default_registry({}).get("noaa")
    assert type(source).__name__ == "NOAAYearlySource"
    assert source.requires_api_key is False
    assert source.is_available() is True


@pytest.mark.parametrize("policy", [None, "", "access", "ghcnd_yearly_v2", True, [], {}])
def test_unknown_explicit_policy_rejected_before_provider_construction(policy, monkeypatch):
    constructor = Mock(side_effect=AssertionError("provider construction before policy validation"))
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.yfinance_source.YFinanceSource", constructor
    )
    with pytest.raises(ValueError, match="invalid_noaa_delivery_policy"):
        build_default_registry({"noaa_delivery_policy": policy})
    constructor.assert_not_called()


def test_yearly_policy_binds_source_identity():
    default_cfg = source_configuration_fingerprint({"autoresearch": {}})
    explicit_cfg = source_configuration_fingerprint(
        {"autoresearch": {"noaa_delivery_policy": "ghcnd_yearly_v1"}}
    )
    assert default_cfg != explicit_cfg
