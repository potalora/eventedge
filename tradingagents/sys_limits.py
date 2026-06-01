"""Process resource-limit helpers.

The daily run is launched by launchd (``com.trading.daily.plist``), which
imposes a soft ``RLIMIT_NOFILE`` of 256 on its job tree. The autoresearch
pipeline fans out across 13 data sources and 12 strategies over dozens of
tickers with concurrent HTTP, so peak open-descriptor usage exceeds 256. When
it does, writes fail with ``OSError: [Errno 24] Too many open files`` — on
2026-06-01 this errored all 16 cohorts after a successful 9-minute fetch.

Raising the soft limit at process startup fixes this. The per-process hard cap
on this machine (``kern.maxfilesperproc``) is 61440, so a floor of 16384 is
comfortably safe while being ~64x the launchd default. ``setrlimit`` here is
inherited by child processes (the per-generation worktree subprocesses spawned
by ``GenerationManager``), so calling it once in the parent entry point covers
the whole run.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Soft RLIMIT_NOFILE floor. ~64x the launchd default of 256, well under the
# macOS per-process hard cap (kern.maxfilesperproc = 61440).
DEFAULT_SOFT_FD_LIMIT = 16384


def target_soft_fd_limit(soft: int, hard: int, desired: int = DEFAULT_SOFT_FD_LIMIT) -> int:
    """Compute the soft RLIMIT_NOFILE to request.

    Raises toward ``desired`` but never lowers an already-higher soft limit and
    never exceeds a finite hard cap.
    """
    try:
        import resource

        infinity = resource.RLIM_INFINITY
    except ImportError:  # non-Unix; resource unavailable
        infinity = -1

    if hard != infinity:
        desired = min(desired, hard)
    return max(soft, desired)


def raise_fd_limit(desired: int = DEFAULT_SOFT_FD_LIMIT) -> int:
    """Raise the soft RLIMIT_NOFILE toward ``desired`` if it is currently lower.

    Returns the soft limit in effect after the call. Never lowers the limit and
    never raises on platforms without ``resource`` or when the OS rejects the
    new value — it logs and continues so a limit problem can never crash a run.
    """
    try:
        import resource
    except ImportError:
        return desired  # nothing we can do on this platform

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = target_soft_fd_limit(soft, hard, desired)
    if target <= soft:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        logger.info("Raised RLIMIT_NOFILE soft limit %d -> %d", soft, target)
        return target
    except (ValueError, OSError) as e:
        logger.warning("Could not raise RLIMIT_NOFILE from %d to %d: %s", soft, target, e)
        return soft
