"""Shared retry-policy helpers for the Django adapter.

`ActivityRun.retry_policy` and the dispatcher's transition-retry config are
both stored/configured as plain dicts (JSONField, constructor kwargs), not
as `ace.RetryPolicy` instances. This module is the single place that turns
those dicts into a `RetryPolicy` so delay computation (including jitter)
stays identical between the activity queue (`queue.py`), the timeout
service (`activity_timeouts.py`), and the workflow dispatcher
(`dispatcher.py`) — no duplicated backoff arithmetic.
"""

from __future__ import annotations

from ace import RetryPolicy


def retry_policy_from_dict(data: dict | None) -> RetryPolicy:
    """Build a `RetryPolicy` from an `ActivityRun.retry_policy`-shaped dict.

    Missing keys fall back to `RetryPolicy` defaults, including
    `jitter_fraction=0.0` (no jitter) for any policy persisted before
    jitter support was added.
    """
    data = data or {}
    defaults = RetryPolicy()
    return RetryPolicy(
        max_attempts=int(data.get("max_attempts", defaults.max_attempts)),
        initial_delay_seconds=float(
            data.get("initial_delay_seconds", defaults.initial_delay_seconds)
        ),
        backoff_multiplier=float(data.get("backoff_multiplier", defaults.backoff_multiplier)),
        max_delay_seconds=float(data.get("max_delay_seconds", defaults.max_delay_seconds)),
        jitter_fraction=float(data.get("jitter_fraction", defaults.jitter_fraction)),
    )
