"""Regression tests for the cumulative statistics chain.

These pin the two correctness invariants VISION principle 5 depends on:

- a transient recorder-read error must never be mistaken for an empty database
  and rewrite the window from zero (fix #1);
- a zero-fill -> repair -> append cycle must keep the cumulative chain
  continuous at the last-DB-hour boundary, losing no energy (fix #2).

Both drive the pure chain-building logic at its smallest seam
(`_write_statistics_chain`), mocking the recorder and the existing-statistics
query so the chain arithmetic is exercised without a live HA database.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.helen_energy_consumption.statistics import (
    HelenConsumptionStatistics,
    StatisticsQueryError,
)

_UTC = ZoneInfo("UTC")
_BASE = datetime(2026, 1, 1, 0, 0, tzinfo=_UTC)
_STATS_MODULE = "custom_components.helen_energy_consumption.statistics"


def _hour(offset: int) -> datetime:
    """Return the UTC hour `offset` hours after the fixed base time."""
    return _BASE + timedelta(hours=offset)


def _entry(offset: int, electricity: float | None) -> SimpleNamespace:
    """Build a fake Helen hourly series entry at the given UTC hour offset."""
    return SimpleNamespace(start=_hour(offset).isoformat(), electricity=electricity)


def _manager() -> HelenConsumptionStatistics:
    """Build a statistics manager with mocked HA and API dependencies."""
    return HelenConsumptionStatistics(MagicMock(), MagicMock(), "12345678", "Helen")


async def test_transient_read_error_does_not_rewrite_from_zero() -> None:
    """A failed existing-statistics query must skip the write, not zero-fill.

    Pre-fix, the query swallowed the error and returned {}, so the chain
    restarted at 0.0 and rewrote the window; this asserts nothing is written.
    """
    manager = _manager()
    manager._import_statistics = AsyncMock()

    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        side_effect=RuntimeError("recorder unavailable")
    )

    series = [_entry(0, 1.0), _entry(1, 1.0), _entry(2, 1.0)]

    with patch(f"{_STATS_MODULE}.get_instance", return_value=recorder):
        # Must not raise and must not write anything.
        await manager._write_statistics_chain(series)

    manager._import_statistics.assert_not_called()


async def test_query_error_propagates_as_sentinel() -> None:
    """The window query raises StatisticsQueryError on a recorder failure."""
    manager = _manager()
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(side_effect=RuntimeError("db locked"))

    with (
        patch(f"{_STATS_MODULE}.get_instance", return_value=recorder),
        pytest.raises(StatisticsQueryError),
    ):
        await manager._get_existing_statistics_in_window(
            manager.consumption_statistic_id, _hour(0), _hour(3)
        )


async def test_repaired_delta_keeps_chain_continuous_at_boundary() -> None:
    """Repairing a zero-filled hour must not drop energy at the append boundary.

    Cycle 1 left H2 zero-filled (cumulative flat at 2.0). Cycle 2 has real
    data for H2 (+0.5) and a new hour H3 (+0.3). The repair cascades +0.5
    forward, so the post-repair DB value at H2 is 2.5; the appended H3 must
    therefore be 2.8, not 2.3. Pre-fix anchored on the stale 2.0 and produced a
    downward step.
    """
    manager = _manager()

    existing = {_hour(0): 1.0, _hour(1): 2.0, _hour(2): 2.0}
    manager._get_existing_statistics_in_window = AsyncMock(return_value=existing)

    captured: dict[str, list] = {}

    async def _capture(stats: list) -> None:
        captured["stats"] = stats

    manager._import_statistics = _capture

    recorder = MagicMock()
    series = [_entry(0, 1.0), _entry(1, 1.0), _entry(2, 0.5), _entry(3, 0.3)]

    with patch(f"{_STATS_MODULE}.get_instance", return_value=recorder):
        await manager._write_statistics_chain(series)

    # The zero-filled hour was repaired with its real delta.
    recorder.async_adjust_statistics.assert_called_once_with(
        manager.consumption_statistic_id, _hour(2), 0.5, "kWh"
    )

    stats = captured["stats"]
    assert len(stats) == 1
    assert stats[0]["start"] == _hour(3)
    # Post-repair boundary is 2.5; appending +0.3 must yield 2.8 (no lost delta).
    assert stats[0]["sum"] == pytest.approx(2.8)
    assert stats[0]["state"] == pytest.approx(2.8)


# --- _convert_to_utc (input edge, issue #3) ---------------------------------


def test_convert_aware_helsinki_winter_to_utc() -> None:
    """An offset-aware winter (EET, +02:00) timestamp converts to UTC."""
    result = _manager()._convert_to_utc("2026-01-15T10:00:00+02:00")
    assert result == datetime(2026, 1, 15, 8, 0, tzinfo=_UTC)


def test_convert_aware_helsinki_summer_to_utc() -> None:
    """An offset-aware summer (EEST, +03:00) timestamp converts to UTC."""
    result = _manager()._convert_to_utc("2026-07-15T10:00:00+03:00")
    assert result == datetime(2026, 7, 15, 7, 0, tzinfo=_UTC)


async def test_convert_naive_localizes_to_helsinki_not_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A naive timestamp is localized to Helsinki regardless of the host tz.

    This is the core regression: with a host tz of America/New_York, a buggy
    astimezone-on-naive would yield 15:00Z; localizing to Helsinki gives 08:00Z.
    """
    manager = _manager()
    await manager._ensure_helsinki_tz()

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        result = manager._convert_to_utc("2026-01-15T10:00:00")
    finally:
        monkeypatch.undo()
        time.tzset()

    assert result == datetime(2026, 1, 15, 8, 0, tzinfo=_UTC)


def test_convert_dst_fallback_aware_buckets_are_distinct() -> None:
    """The two offsets of the DST fall-back hour map to distinct UTC hours."""
    before = _manager()._convert_to_utc("2025-10-26T03:00:00+03:00")
    after = _manager()._convert_to_utc("2025-10-26T03:00:00+02:00")
    assert before == datetime(2025, 10, 26, 0, 0, tzinfo=_UTC)
    assert after == datetime(2025, 10, 26, 1, 0, tzinfo=_UTC)
    assert before != after


async def test_convert_naive_dst_fallback_collapses_via_fold_zero() -> None:
    """A naive fall-back local hour collapses to its first (fold=0) offset.

    Documents the unrecoverable ambiguity noted in _convert_to_utc: without an
    offset the duplicated Helsinki local hour resolves to EEST (+03:00).
    """
    manager = _manager()
    await manager._ensure_helsinki_tz()
    result = manager._convert_to_utc("2025-10-26T03:30:00")
    assert result == datetime(2025, 10, 26, 0, 30, tzinfo=_UTC)
