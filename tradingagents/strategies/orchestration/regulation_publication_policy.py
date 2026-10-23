"""Explicit prospective research boundary for linked regulatory publications."""
from __future__ import annotations

from collections.abc import Mapping

POLICY = 'linked_publications_v1'
SOURCE_VERSION = 'regulation-publication-source-v1'


def configured(config):
    """Validate ar-only policy dependencies; absence preserves legacy behavior."""
    policy = config.get('regulation_publication_policy')
    if policy is None:
        if config.get('regulation_retrieval_policy') == 'verified_government_copy_v4':
            raise ValueError('invalid_regulation_publication_policy')
        return False
    if (policy != POLICY
            or config.get('regulation_evidence_policy') != 'complete_document_text_v1'
            or config.get('regulation_retrieval_policy') != 'verified_government_copy_v4'
            or config.get('decision_clock_policy') != 'prospective_next_open_v1'):
        raise ValueError('invalid_regulation_publication_policy')
    return True


def source_required(config, strategies):
    """New linked research acquires only for an enabled source consumer."""
    if not configured(config):
        return True  # retain the legacy acquisition/validation contract
    disabled = config.get('disabled_strategies', {})
    excluded = config.get('disabled_strategy_capabilities', {})
    return any('regulations' in strategy.data_sources
        and not disabled.get(strategy.name)
        and not excluded.get(strategy.name, {}).get('regulations')
        and not getattr(strategy, 'retirement_reason', None)
        for strategy in strategies)


def validate_configuration(config):
    """Require the research policy's full configuration before state creation."""
    if not isinstance(config, Mapping) or not isinstance(config.get('autoresearch', {}), Mapping):
        raise ValueError('invalid_regulation_publication_policy')
    if not configured(config.get('autoresearch', {})):
        return False
    execution = config.get('execution')
    if (not isinstance(execution, Mapping) or execution.get('mode') != 'paper'
            or execution.get('execution_enabled') is not False):
        raise ValueError('invalid_regulation_publication_policy')
    return True


def source_configuration(graph, frozen_config=None):
    """Bind linked reports to independently retained generation configuration.

    Legacy reports retain their historical reconstruction path. A linked graph
    cannot authorize its own policy, clock, or execution scope.
    """
    declared = frozen_config.get('autoresearch', {}) if isinstance(frozen_config, Mapping) else {}
    linked = validate_configuration(frozen_config) if frozen_config is not None else False
    graph_linked = isinstance(graph, Mapping) and (
        graph.get('publication_policy') is not None or graph.get('version') == SOURCE_VERSION)
    if linked or graph_linked:
        if not linked:
            raise ValueError('invalid_regulation_publication_policy')
        if graph is not None and (not isinstance(graph, Mapping)
                or graph.get('version') != SOURCE_VERSION
                or graph.get('publication_policy') != declared['regulation_publication_policy']
                or graph.get('policy') != declared['regulation_evidence_policy']
                or graph.get('retrieval_policy') != declared['regulation_retrieval_policy']):
            raise ValueError('invalid_regulation_publication_policy')
        return dict(declared)
    result = {'regulation_evidence_policy': graph.get('policy')} if isinstance(graph, Mapping) else {}
    if isinstance(graph, Mapping) and 'retrieval_policy' in graph:
        result['regulation_retrieval_policy'] = graph['retrieval_policy']
    return result
