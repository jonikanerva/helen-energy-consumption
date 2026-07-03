"""Smoke tests for the integration's constants.

These pin the poll cadence and rolling-window shape described in STACK.md so an
accidental change to either is caught by the test harness.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.helen_energy_consumption import const


def test_domain() -> None:
    assert const.DOMAIN == "helen_energy_consumption"


def test_poll_interval_is_three_hours() -> None:
    assert const.SCAN_INTERVAL == timedelta(hours=3)


def test_backfill_window_is_seven_days() -> None:
    assert const.STATISTICS_BACKFILL_HOURS == 168
