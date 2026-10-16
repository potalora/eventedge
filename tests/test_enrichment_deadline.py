"""Native enrichment inherits the original shared model-phase clock."""
from test_source_reliability_pipeline import pipeline, SESSION


def test_daily_enrichment_receives_original_absolute_model_deadline(pipeline, monkeypatch):
    from tradingagents.strategies.runtime_deadline import current_model_deadline
    from tradingagents.strategies.data_sources.request_policy import current_provider_deadline
    fixture, owner, _, _ = pipeline
    screening_deadlines, enrichment_deadlines = [], []
    screen = owner._screen_for_horizon

    def observed_screen(*args, **kwargs):
        screening_deadlines.append(current_model_deadline())
        return screen(*args, **kwargs)

    def observed_enrichment(signals):
        enrichment_deadlines.append(current_provider_deadline("openbb"))
        return {}

    monkeypatch.setattr(owner, "_screen_for_horizon", observed_screen)
    monkeypatch.setattr(owner, "_fetch_openbb_enrichment", observed_enrichment)
    result = owner.run_daily(str(SESSION))
    assert len(result) == 16
    assert len(screening_deadlines) == 4
    assert screening_deadlines[0] is not None
    assert len(set(screening_deadlines)) == 1
    assert enrichment_deadlines == [screening_deadlines[0]]
