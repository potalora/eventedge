"""Fail-closed production policy for retired learning automation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LearningPolicy:
    """The sole production policy: automated learning is disabled."""

    mode: Literal["disabled"] = "disabled"

    def __post_init__(self) -> None:
        if self.mode != "disabled":
            raise ValueError(
                "production learning is disabled; only mode='disabled' is accepted"
            )
