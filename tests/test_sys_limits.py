"""Tests for tradingagents.sys_limits — file-descriptor limit handling.

Regression coverage for the 2026-06-01 incident: the daily run is launched by
launchd, which imposes a soft RLIMIT_NOFILE of 256. The 12-strategy / 13-source
fetch fan-out exceeds 256 concurrent descriptors, so writes failed with
OSError [Errno 24] Too many open files and all 16 cohorts errored. The fix
raises the soft limit at process startup.
"""
from __future__ import annotations

import resource

from tradingagents.sys_limits import (
    DEFAULT_SOFT_FD_LIMIT,
    raise_fd_limit,
    target_soft_fd_limit,
)


def test_raises_low_launchd_limit():
    """The launchd default of 256 must be raised to the desired floor."""
    assert target_soft_fd_limit(256, resource.RLIM_INFINITY) == DEFAULT_SOFT_FD_LIMIT
    assert target_soft_fd_limit(256, resource.RLIM_INFINITY) > 256


def test_never_lowers_an_already_high_limit():
    """An interactive shell may already grant 1048576 — never reduce it."""
    high = 1_048_576
    assert target_soft_fd_limit(high, resource.RLIM_INFINITY) == high


def test_respects_finite_hard_cap():
    """Never request a soft limit above the hard cap."""
    assert target_soft_fd_limit(256, 4096, desired=16384) == 4096
    # If hard is already below the desired floor, cap at hard.
    assert target_soft_fd_limit(256, 1024) == 1024


def test_custom_desired_floor():
    assert target_soft_fd_limit(256, resource.RLIM_INFINITY, desired=8192) == 8192


def test_raise_fd_limit_is_idempotent_and_nonlowering():
    """Calling raise_fd_limit() must leave the soft limit >= the floor and
    never lower whatever was already in effect."""
    before_soft, before_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    new_soft = raise_fd_limit()
    after_soft, after_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert after_soft == new_soft
    assert after_soft >= before_soft
    expected_floor = min(
        DEFAULT_SOFT_FD_LIMIT,
        before_hard if before_hard != resource.RLIM_INFINITY else DEFAULT_SOFT_FD_LIMIT,
    )
    assert after_soft >= expected_floor
    # Restore original limit so the test is side-effect free.
    resource.setrlimit(resource.RLIMIT_NOFILE, (before_soft, before_hard))
