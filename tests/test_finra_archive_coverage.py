"""Native archive download failures cannot certify current or historical scope."""
from datetime import datetime

import pytest

from test_finra_bulk import native, raw
from tradingagents.strategies.data_sources import finra_bulk
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

ACQUIRED = datetime.fromisoformat('2026-10-22T12:00:00+00:00')


def test_silent_native_404_for_current_archive_fails_acquisition(native):
    native.add([raw('A', settlementDate='2026-09-15')])
    native.storage.get_data_from_date_and_store = lambda compact: None
    attempt = finra_bulk.new_attempt()
    with pytest.raises(SourceFetchError) as error:
        finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    assert error.value.reason_code == 'provider_error'
    assert attempt['archive_coverage']['current_complete'] is False
    assert attempt['missing_archive_dates'] == ['2026-09-30']
    assert attempt['acquired_at'] == ACQUIRED.isoformat()


def test_missing_current_stops_before_optional_history_downloads(native):
    native.storage.get_short_interest_dates = lambda: ['20240112', '20260930']
    requested = []
    def download(compact):
        requested.append(compact)
        assert compact == '20260930', 'History must not consume the failed-current budget'
    native.storage.get_data_from_date_and_store = download
    with pytest.raises(SourceFetchError):
        finra_bulk.acquire(['A'], finra_bulk.new_attempt(), acquired_at=ACQUIRED)
    assert requested == ['20260930']


def test_corrected_native_holiday_inventory_does_not_request_nonexistent_files(native):
    native.storage.get_short_interest_dates = lambda: ['20240115', '20240329', '20260930']
    native.add([raw('A', settlementDate='2024-01-12'), raw('A', settlementDate='2024-03-28'), raw('A')])
    downloaded = []
    native.storage.get_data_from_date_and_store = downloaded.append
    attempt = finra_bulk.new_attempt()
    _, histories = finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    assert downloaded == []
    assert attempt['required_archive_dates'] == ['2024-01-12', '2024-03-28', '2026-09-30']
    assert attempt['archive_coverage']['historical_complete'] is True
    assert len(histories['A']) == 3


def test_current_cycle_download_skips_optional_history(native):
    downloaded = []
    def download(compact):
        downloaded.append(compact)
        if compact == '20260930':
            native.add([raw('A')])
    native.storage.get_data_from_date_and_store = download
    attempt = finra_bulk.new_attempt()
    _, histories = finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    assert downloaded == ['20260930']
    assert histories['A'][0]['settlementDate'] == '2026-09-30'
    assert attempt['archive_coverage']['current_complete'] is True
    assert attempt['archive_coverage']['historical_complete'] is False


def test_historical_backfill_cannot_consume_current_query_budget(native):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    native.add([raw('A')])
    clock = [0.0]
    def slow_history(compact):
        clock[0] = 20.0
    native.storage.get_data_from_date_and_store = slow_history
    with provider_budget('openbb', 15, clock=lambda: clock[0], limits=()):
        attempt = finra_bulk.new_attempt()
        _, rows = finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    assert len(rows['A']) == 1 and clock[0] == 0
    assert attempt['archive_coverage']['current_complete'] is True
    assert attempt['archive_coverage']['historical_complete'] is False


def test_version_two_partial_history_evidence_is_durable_and_tamper_checked(native):
    native.add([raw('A')])
    native.storage.get_data_from_date_and_store = lambda compact: None
    attempt = finra_bulk.new_attempt()
    finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    attempt.update(status='partial', reason_code='provider_error')
    metadata = dict(schema_version=2, requested_count=1, cached_count=0,
                    population_sha256=finra_bulk.population_digest(['A']), attempts=[attempt])
    assert finra_bulk.validated_acquisition(metadata) == metadata
    attempt['archive_coverage']['historical_complete'] = True
    with pytest.raises(ValueError):
        finra_bulk.validated_acquisition(metadata)


def test_archived_version_one_metadata_retains_its_original_schema(native):
    native.add([raw('A'), raw('A', settlementDate='2026-09-15')])
    attempt = finra_bulk.new_attempt()
    finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    attempt.update(status='success', reason_code=None)
    attempt.pop('archive_coverage')
    attempt.pop('acquired_at')
    legacy = dict(schema_version=1, requested_count=1, cached_count=0,
                  population_sha256=finra_bulk.population_digest(['A']), attempts=[attempt])
    assert finra_bulk.validated_acquisition(legacy) == legacy
