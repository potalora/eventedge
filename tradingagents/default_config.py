"""Clean default configuration backed by typed GenerationConfig."""

from __future__ import annotations

from tradingagents.strategies.config import GenerationConfig

DEFAULT_GENERATION_CONFIG: GenerationConfig = GenerationConfig()
DEFAULT_CONFIG: dict = DEFAULT_GENERATION_CONFIG.to_runtime_dict()

__all__ = ["DEFAULT_CONFIG", "DEFAULT_GENERATION_CONFIG"]
