"""Cron expression math — a thin wrapper around ``croniter``.

Kept as a separate module (rather than inlined in ``runner.py`` or
``manager.py``) so both can share it and so the one external dependency
this feature adds (``croniter`` — parses/evaluates 5-field cron
expressions; nothing in the standard library does this correctly,
including leap years and day-of-week edge cases) has a single call
site to swap out if that ever changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mlops_framework.exceptions import InvalidCronExpressionError


def validate(cron_expression: str) -> None:
    """Raise :class:`InvalidCronExpressionError` if not valid 5-field cron.

    Called at schedule create/update time so a typo is caught
    immediately rather than silently never firing.
    """
    from croniter import CroniterBadCronError, croniter

    if not croniter.is_valid(cron_expression):
        raise InvalidCronExpressionError(
            f"{cron_expression!r} is not a valid 5-field cron expression "
            "(minute hour day month day-of-week)"
        )
    try:
        # is_valid() checks syntax; constructing it also catches a field
        # combination is_valid() lets through but croniter can't actually
        # iterate (e.g. a day-of-month/month pair that never occurs).
        croniter(cron_expression, datetime.now(timezone.utc))
    except (CroniterBadCronError, ValueError) as exc:
        raise InvalidCronExpressionError(
            f"{cron_expression!r} is not a usable cron expression: {exc}"
        ) from exc


def next_fire_time(cron_expression: str, after: datetime) -> datetime:
    """Return the next time ``cron_expression`` fires strictly after ``after``."""
    from croniter import croniter

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    return croniter(cron_expression, after).get_next(datetime)


def is_due(
    cron_expression: str,
    *,
    last_triggered_at: datetime | None,
    created_at: datetime,
    now: datetime,
) -> bool:
    """Whether a schedule should fire now.

    Anchored at ``last_triggered_at`` once it has fired at least once,
    or at ``created_at`` before that — so a schedule waits for its
    *next* natural occurrence after being created rather than firing
    immediately because some earlier-in-the-day slot has already
    passed (a schedule created at 2:55am for a 2am-daily cron should
    train tomorrow at 2am, not the moment it's saved). Standard cron
    semantics: nothing catches up on occurrences that were missed
    before the job existed, the same way ``catchup=False`` works in
    Airflow.
    """
    anchor = last_triggered_at or created_at
    return next_fire_time(cron_expression, anchor) <= now
