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

import asyncio
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.helen_energy_consumption.statistics import (
    HelenConsumptionStatistics,
    StatisticsQueryError,
    _chain_lock,
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


def _manager_for(delivery_site_id: str) -> HelenConsumptionStatistics:
    """Build a manager keyed on delivery_site_id (its shared chain lock)."""
    return HelenConsumptionStatistics(
        MagicMock(), MagicMock(), delivery_site_id, "Helen"
    )


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
    recorder.async_block_till_done = AsyncMock()  # GUARD #2 flush after repairs
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


# --- mid-window interior gap repair (issue #6) ------------------------------


async def _run_poll(
    manager: HelenConsumptionStatistics,
    series: list[SimpleNamespace],
    existing: dict[datetime, float],
    recorder: MagicMock | None = None,
) -> tuple[list | None, MagicMock]:
    """Run one _write_statistics_chain poll with a mocked existing snapshot.

    Returns the captured import batch (or None if nothing was imported) and the
    recorder mock used for repair adjustments.
    """
    manager._get_existing_statistics_in_window = AsyncMock(return_value=existing)
    captured: dict[str, list] = {}

    async def _capture(stats: list) -> None:
        captured["stats"] = stats

    manager._import_statistics = _capture
    rec = recorder or MagicMock()
    rec.async_block_till_done = AsyncMock()  # GUARD #2 flush after repairs
    with patch(f"{_STATS_MODULE}.get_instance", return_value=rec):
        await manager._write_statistics_chain(series)
    return captured.get("stats"), rec


async def test_missing_interior_hour_flat_filled_this_poll() -> None:
    """A fully-absent interior hour is flat-filled now, not adjusted."""
    manager = _manager()
    # H2 is entirely absent between present H1 and H3; no zero-filled hours.
    existing = {_hour(0): 1.0, _hour(1): 2.0, _hour(3): 2.0}

    stats, recorder = await _run_poll(manager, [_entry(1, 1.0)], existing)

    assert stats is not None
    rows = {row["start"]: row["sum"] for row in stats}
    assert rows == {_hour(2): pytest.approx(2.0)}
    # Flat row holds the sum flat from the left neighbour -> chain stays monotonic.
    assert existing[_hour(1)] <= rows[_hour(2)] <= existing[_hour(3)]
    # The gap hour is not adjusted this poll (deferred to the next poll).
    recorder.async_adjust_statistics.assert_not_called()
    # No repair -> no recorder flush (GUARD #2 flush is scoped to repair cycles).
    recorder.async_block_till_done.assert_not_called()


async def test_missing_interior_hour_converges_next_poll() -> None:
    """Next poll repairs the now-present flat hour and anchors on the delta."""
    manager = _manager()
    # H2 is now present as a flat row (2.0) from the previous poll; H4 is new.
    existing = {_hour(1): 2.0, _hour(2): 2.0, _hour(3): 2.0}

    stats, recorder = await _run_poll(
        manager, [_entry(2, 0.6), _entry(4, 0.3)], existing
    )

    # Adjacent-pair repair applies the API delta to the once-missing hour.
    recorder.async_adjust_statistics.assert_called_once_with(
        manager.consumption_statistic_id, _hour(2), 0.6, "kWh"
    )
    # A repair was applied -> the GUARD #2 flush runs before releasing the lock.
    recorder.async_block_till_done.assert_awaited_once()
    # New tail hour anchors on existing[H3] + repaired_delta (2.0 + 0.6), so the
    # appended H4 is 2.6 + 0.3 = 2.9 — the repaired energy is not lost.
    assert stats is not None
    rows = {row["start"]: row["sum"] for row in stats}
    assert rows[_hour(4)] == pytest.approx(2.9)


async def test_repair_before_gap_stays_monotonic_multi_repair() -> None:
    """da's guard: repairs before a gap must lift the flat fill (no dip).

    Two zero-filled hours (H1, H2) are repaired (+0.5, +0.7) before a
    fully-absent gap at H3 whose present neighbours are H2 and H4. The repair
    cascade leaves H2 at 1.0 + 0.5 + 0.7 = 2.2, so the flat H3 must be 2.2 too.
    A naive pre-repair snapshot would fill H3 with 1.0 -> a permanent dip.
    """
    manager = _manager()
    existing = {_hour(0): 1.0, _hour(1): 1.0, _hour(2): 1.0, _hour(4): 1.0}

    stats, recorder = await _run_poll(
        manager, [_entry(1, 0.5), _entry(2, 0.7)], existing
    )

    # Both zero-filled hours before the gap were repaired; the gap was not.
    assert recorder.async_adjust_statistics.call_count == 2
    adjusted_hours = {
        c.args[1] for c in recorder.async_adjust_statistics.call_args_list
    }
    assert adjusted_hours == {_hour(1), _hour(2)}

    assert stats is not None
    rows = {row["start"]: row["sum"] for row in stats}
    # Flat fill reflects ALL repair deltas applied at hours <= prev_hour (H2):
    # 1.0 + 0.5 + 0.7 = 2.2, matching the post-cascade neighbours (no dip).
    assert rows[_hour(3)] == pytest.approx(2.2)


async def test_transient_read_failure_skips_gap_fill() -> None:
    """A failed existing read writes nothing, even with an interior gap."""
    manager = _manager()
    manager._import_statistics = AsyncMock()

    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(side_effect=RuntimeError("db down"))

    # Series that would otherwise trigger a gap fill / append.
    series = [_entry(0, 1.0), _entry(3, 0.4)]
    with patch(f"{_STATS_MODULE}.get_instance", return_value=recorder):
        await manager._write_statistics_chain(series)

    manager._import_statistics.assert_not_called()


async def test_combined_batch_is_disjoint_and_ordered() -> None:
    """missing_rows (< last_db_hour) and appended stats (>= +1h) are disjoint."""
    manager = _manager()
    # Gap at H1 (between H0 and H2); H3 is a new tail hour to append.
    existing = {_hour(0): 1.0, _hour(2): 1.0}

    stats, _ = await _run_poll(manager, [_entry(0, 1.0), _entry(3, 0.4)], existing)

    assert stats is not None
    starts = [row["start"] for row in stats]
    last_db_hour = max(existing.keys())
    missing = [h for h in starts if h < last_db_hour]
    appended = [h for h in starts if h >= last_db_hour + timedelta(hours=1)]
    assert missing == [_hour(1)]
    assert appended == [_hour(3)]
    assert set(missing).isdisjoint(appended)
    # Concatenation is already globally ascending.
    assert starts == sorted(starts)
    rows = {row["start"]: row["sum"] for row in stats}
    assert rows[_hour(1)] == pytest.approx(1.0)  # flat from H0
    assert rows[_hour(3)] == pytest.approx(1.4)  # 1.0 anchor + 0.4


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


# --- rebuild_range (backfill action, issue #10) -----------------------------


def _ser(hour: datetime, electricity: float | None) -> SimpleNamespace:
    """Build a fake Helen series entry at an absolute UTC hour."""
    return SimpleNamespace(start=hour.isoformat(), electricity=electricity)


async def _start_utc(manager: HelenConsumptionStatistics, start_date: date) -> datetime:
    """Compute start_utc the way rebuild_range does (Helsinki 00:00 -> UTC)."""
    await manager._ensure_helsinki_tz()
    return manager._convert_to_utc(
        datetime.combine(start_date, datetime.min.time()).isoformat()
    ).replace(minute=0, second=0, microsecond=0)


async def _run_rebuild(
    manager: HelenConsumptionStatistics,
    start_date: date,
    series: list[SimpleNamespace],
    in_range: dict[datetime, float],
    anchor_rows: dict[datetime, float],
    now_hour: datetime,
) -> list | None:
    """Drive rebuild_range with fully mocked fetch/reads; return the write batch."""
    captured: dict[str, list] = {}

    async def _capture(rows: list) -> None:
        captured["rows"] = rows

    manager._import_statistics = _capture
    manager._fetch_range_data = AsyncMock(return_value=series)

    async def _read(stat_id: str, start: datetime, end: datetime | None) -> dict:
        # end=None -> the in-range tail read; otherwise the strict pre-start read.
        return dict(in_range) if end is None else dict(anchor_rows)

    manager._get_existing_statistics_in_window = _read

    with patch(f"{_STATS_MODULE}.dt_util.utcnow", return_value=now_hour):
        await manager.rebuild_range(start_date)
    return captured.get("rows")


async def test_rebuild_continuous_at_both_boundaries() -> None:
    """A clean rebuild anchors on the prior sum and stays contiguous/monotonic."""
    manager = _manager()
    start_date = date(2026, 1, 15)
    s0 = await _start_utc(manager, start_date)

    def h(i: int) -> datetime:
        return s0 + timedelta(hours=i)

    series = [_ser(h(i), e) for i, e in enumerate([1.0, 2.0, 3.0, 4.0, 5.0])]
    anchor_rows = {h(-1): 10.0}  # pre-anchor sum before start

    rows = await _run_rebuild(manager, start_date, series, {}, anchor_rows, h(4))

    assert rows is not None
    starts = [r["start"] for r in rows]
    sums = [r["sum"] for r in rows]
    assert all(st >= s0 for st in starts)  # nothing before start_utc
    assert starts == [h(i) for i in range(5)]  # contiguous
    assert sums[0] == pytest.approx(11.0)  # 10 anchor + elec0
    assert sums == sorted(sums)  # monotonic
    assert sums[-1] == pytest.approx(25.0)


async def test_rebuild_onboarding_starts_from_zero() -> None:
    """With no prior data the anchor is 0.0 and the first row is elec0."""
    manager = _manager()
    start_date = date(2026, 1, 15)
    s0 = await _start_utc(manager, start_date)

    def h(i: int) -> datetime:
        return s0 + timedelta(hours=i)

    series = [_ser(h(i), e) for i, e in enumerate([1.5, 2.0, 0.5])]

    rows = await _run_rebuild(manager, start_date, series, {}, {}, h(2))

    assert rows is not None
    assert rows[0]["sum"] == pytest.approx(1.5)
    assert rows[-1]["sum"] == pytest.approx(4.0)


async def test_rebuild_first_hour_predecessor_is_anchor() -> None:
    """GUARD #1: h0's predecessor is the anchor, not an absent in_range[h0-1]."""
    manager = _manager()
    start_date = date(2026, 1, 15)
    s0 = await _start_utc(manager, start_date)

    def h(i: int) -> datetime:
        return s0 + timedelta(hours=i)

    # API has no data for h0; the DB already holds a real h0 (sum 12 = anchor 10
    # + delta 2). Using the anchor as predecessor preserves it as 12; a naive
    # in_range[h0-1] lookup (absent -> 0) would double it to 22.
    series = [_ser(h(1), 3.0), _ser(h(2), 4.0)]
    in_range = {h(0): 12.0}
    anchor_rows = {h(-1): 10.0}

    rows = await _run_rebuild(manager, start_date, series, in_range, anchor_rows, h(2))

    assert rows is not None
    by_start = {r["start"]: r["sum"] for r in rows}
    assert by_start[h(0)] == pytest.approx(12.0)  # preserved via anchor predecessor
    assert by_start[h(1)] == pytest.approx(15.0)  # 12 + 3
    assert by_start[h(2)] == pytest.approx(19.0)  # 15 + 4


async def test_rebuild_preserves_real_history_with_missing_predecessor_gaps() -> None:
    """Partial API response preserves the existing real tail across gaps.

    API returns h0-h4 (matching the existing sums); h5-h10 come back None.
    The existing DB tail has gaps at h7 and h9. Each preserved hour measures its
    delta against the RUNNING cumulative, so the chain snaps back onto the
    existing sums (h10 unchanged) even across the absent predecessors — a naive
    in_range[h-1] lookup would double-count at h8 and h10.
    """
    manager = _manager()
    start_date = date(2026, 1, 15)
    s0 = await _start_utc(manager, start_date)

    def h(i: int) -> datetime:
        return s0 + timedelta(hours=i)

    series = [_ser(h(i), 1.0) for i in range(5)]  # h0-h4 real; cumulative -> 5
    series += [_ser(h(i), None) for i in range(5, 11)]  # h5-h10 no API data
    in_range = {
        h(0): 1.0,
        h(1): 2.0,
        h(2): 3.0,
        h(3): 4.0,
        h(4): 5.0,
        h(5): 6.0,
        h(6): 7.0,
        h(8): 9.0,
        h(10): 11.0,  # h7, h9 absent (gaps)
    }

    rows = await _run_rebuild(manager, start_date, series, in_range, {}, h(10))

    assert rows is not None
    by_start = {r["start"]: r["sum"] for r in rows}
    assert by_start[h(5)] == pytest.approx(6.0)
    assert by_start[h(6)] == pytest.approx(7.0)
    assert by_start[h(7)] == pytest.approx(7.0)  # gap: flat-held
    assert by_start[h(8)] == pytest.approx(9.0)  # snaps back despite h7 absent
    assert by_start[h(9)] == pytest.approx(9.0)  # gap: flat-held
    assert by_start[h(10)] == pytest.approx(11.0)  # tail unchanged
    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums)  # monotonic


async def test_rebuild_fetch_failure_writes_nothing() -> None:
    """A transient fetch failure raises before the write; nothing is imported."""
    manager = _manager()
    manager._import_statistics = AsyncMock()
    manager._fetch_range_data = AsyncMock(side_effect=RuntimeError("Helen down"))

    with pytest.raises(RuntimeError):
        await manager.rebuild_range(date(2026, 1, 15))

    manager._import_statistics.assert_not_called()


async def test_rebuild_empty_response_writes_nothing() -> None:
    """An empty API response raises and imports nothing (fail-closed).

    Reads and utcnow are mocked/bounded so that without the empty-response guard
    the code would reach the (bounded) write — this asserts it does not.
    """
    manager = _manager()
    start_date = date(2026, 1, 15)
    s0 = await _start_utc(manager, start_date)

    manager._import_statistics = AsyncMock()
    manager._fetch_range_data = AsyncMock(return_value=[])

    async def _read(stat_id: str, start: datetime, end: datetime | None) -> dict:
        return {}

    manager._get_existing_statistics_in_window = _read

    with (
        patch(f"{_STATS_MODULE}.dt_util.utcnow", return_value=s0 + timedelta(hours=2)),
        pytest.raises(HomeAssistantError),
    ):
        await manager.rebuild_range(start_date)

    manager._import_statistics.assert_not_called()


# --- shared per-chain write lock (issue #18) --------------------------------


class _FakeRecorder:
    """Model HA's queued-write / separate-read-thread behaviour for repairs.

    async_adjust_statistics enqueues a non-idempotent `sum += adj` but does NOT
    apply it; async_block_till_done (the GUARD #2 flush) applies the queue. Reads
    go through the manager's mocked _get_existing_statistics_in_window against
    `db`, so a reader only observes applied (flushed) adjustments.
    """

    def __init__(self, db: dict[datetime, float]) -> None:
        self.db = db
        self._pending: list[tuple[datetime, float]] = []
        self.adjust_calls: list[tuple[datetime, float]] = []

    def async_adjust_statistics(
        self, statistic_id: str, start: datetime, adj: float, unit: str
    ) -> None:
        self.adjust_calls.append((start, adj))
        self._pending.append((start, adj))  # queued, not yet applied

    async def async_block_till_done(self) -> None:
        for start, adj in self._pending:
            for hour in self.db:
                if hour >= start:  # adjust cascades forward
                    self.db[hour] += adj
        self._pending.clear()


def _db_reader(recorder: _FakeRecorder):
    """A _get_existing_statistics_in_window stand-in reading applied DB state."""

    async def _read(statistic_id: str, start: datetime, end: datetime | None) -> dict:
        return dict(recorder.db)

    return _read


async def test_reload_repair_applied_exactly_once() -> None:
    """GUARD #2: a repaired hour is adjusted once across two same-chain cycles.

    Models the reload case (two coordinators, one chain). Without the post-repair
    flush the second cycle reads the stale, still-flat chain and re-enqueues the
    same +delta -> a permanent double-count.
    """
    db = {_hour(0): 1.0, _hour(1): 1.0, _hour(2): 2.0}  # H1 zero-filled (flat)
    recorder = _FakeRecorder(db)
    old = _manager_for("reload-site")
    new = _manager_for("reload-site")  # same delivery_site_id -> same chain lock
    for mgr in (old, new):
        mgr._get_existing_statistics_in_window = _db_reader(recorder)
        mgr._import_statistics = AsyncMock()

    series = [_entry(0, 1.0), _entry(1, 0.5), _entry(2, 1.0)]  # H1 now has data
    with patch(f"{_STATS_MODULE}.get_instance", return_value=recorder):
        await old._write_statistics_chain(series)
        await new._write_statistics_chain(series)

    h1_adjusts = [call for call in recorder.adjust_calls if call[0] == _hour(1)]
    assert len(h1_adjusts) == 1


async def test_same_statistic_id_serializes() -> None:
    """A second writer of the same chain waits for the first to release."""
    first = _manager_for("serialize-site")
    second = _manager_for("serialize-site")
    await first._ensure_helsinki_tz()
    await second._ensure_helsinki_tz()

    first_inside = asyncio.Event()
    release = asyncio.Event()
    second_read = asyncio.Event()

    async def _first_read(*_args) -> dict:
        first_inside.set()
        await release.wait()  # hold the chain lock
        return {}

    first._get_existing_statistics_in_window = _first_read
    first._import_statistics = AsyncMock()

    async def _second_read(*_args) -> dict:
        second_read.set()
        return {}

    second._get_existing_statistics_in_window = _second_read
    second._import_statistics = AsyncMock()

    series = [_entry(0, 1.0)]
    with patch(f"{_STATS_MODULE}.get_instance", return_value=MagicMock()):
        task_first = asyncio.create_task(first._write_statistics_chain(series))
        await first_inside.wait()  # first holds the chain lock

        task_second = asyncio.create_task(second._write_statistics_chain(series))
        await asyncio.sleep(0)
        assert not second_read.is_set()  # blocked on the shared lock
        assert _chain_lock(first.consumption_statistic_id).locked()

        release.set()
        await task_first
        await task_second
        assert second_read.is_set()  # proceeded once the first released


async def test_different_statistic_ids_do_not_block() -> None:
    """A writer of a different chain is not blocked by another chain's holder."""
    blocker = _manager_for("site-x")
    other = _manager_for("site-y")
    await blocker._ensure_helsinki_tz()
    await other._ensure_helsinki_tz()

    blocker_inside = asyncio.Event()
    release = asyncio.Event()

    async def _blocker_read(*_args) -> dict:
        blocker_inside.set()
        await release.wait()
        return {}

    blocker._get_existing_statistics_in_window = _blocker_read
    blocker._import_statistics = AsyncMock()
    other._get_existing_statistics_in_window = AsyncMock(return_value={})
    other._import_statistics = AsyncMock()

    series = [_entry(0, 1.0)]
    with patch(f"{_STATS_MODULE}.get_instance", return_value=MagicMock()):
        task_blocker = asyncio.create_task(blocker._write_statistics_chain(series))
        await blocker_inside.wait()

        # Different chain lock -> runs to completion without waiting.
        await other._write_statistics_chain(series)
        other._import_statistics.assert_awaited_once()

        release.set()
        await task_blocker


async def test_backfill_and_poll_serialize_on_same_chain() -> None:
    """rebuild_range and a poll of the same chain contend the same lock."""
    backfiller = _manager_for("shared-site")
    poller = _manager_for("shared-site")
    await backfiller._ensure_helsinki_tz()
    await poller._ensure_helsinki_tz()

    backfill_inside = asyncio.Event()
    release = asyncio.Event()
    poll_read = asyncio.Event()

    async def _backfill_read(*_args) -> dict:
        backfill_inside.set()
        await release.wait()  # hold the chain lock inside rebuild_range
        return {}

    backfiller._fetch_range_data = AsyncMock(return_value=[_entry(0, 1.0)])
    backfiller._get_existing_statistics_in_window = _backfill_read
    backfiller._get_anchor_sum = AsyncMock(return_value=0.0)
    backfiller._import_statistics = AsyncMock()

    async def _poll_read(*_args) -> dict:
        poll_read.set()
        return {}

    poller._get_existing_statistics_in_window = _poll_read
    poller._import_statistics = AsyncMock()

    with patch(f"{_STATS_MODULE}.get_instance", return_value=MagicMock()):
        task_backfill = asyncio.create_task(backfiller.rebuild_range(date(2026, 1, 15)))
        await backfill_inside.wait()

        task_poll = asyncio.create_task(
            poller._write_statistics_chain([_entry(0, 1.0)])
        )
        await asyncio.sleep(0)
        assert not poll_read.is_set()  # poll blocked on the shared chain lock

        release.set()
        await task_backfill
        await task_poll
        assert poll_read.is_set()


async def test_chain_lock_released_on_query_error() -> None:
    """A StatisticsQueryError early-return releases the lock for the next writer."""
    first = _manager_for("err-site")
    second = _manager_for("err-site")
    first._get_existing_statistics_in_window = AsyncMock(
        side_effect=StatisticsQueryError("boom")
    )
    first._import_statistics = AsyncMock()
    second._get_existing_statistics_in_window = AsyncMock(return_value={})
    second._import_statistics = AsyncMock()

    series = [_entry(0, 1.0)]
    with patch(f"{_STATS_MODULE}.get_instance", return_value=MagicMock()):
        await first._write_statistics_chain(series)  # returns; no wedge
        await second._write_statistics_chain(series)

    second._import_statistics.assert_awaited_once()
    assert not _chain_lock(first.consumption_statistic_id).locked()


async def test_chain_lock_released_on_exception() -> None:
    """An exception inside the critical section still releases the lock."""
    first = _manager_for("raise-site")
    second = _manager_for("raise-site")
    first._get_existing_statistics_in_window = AsyncMock(return_value={})
    first._import_statistics = AsyncMock(side_effect=RuntimeError("write failed"))
    second._get_existing_statistics_in_window = AsyncMock(return_value={})
    second._import_statistics = AsyncMock()

    series = [_entry(0, 1.0)]
    with patch(f"{_STATS_MODULE}.get_instance", return_value=MagicMock()):
        with pytest.raises(RuntimeError):
            await first._write_statistics_chain(series)
        await second._write_statistics_chain(series)

    second._import_statistics.assert_awaited_once()
    assert not _chain_lock(second.consumption_statistic_id).locked()


def test_chain_lock_identity() -> None:
    """_chain_lock returns one shared lock per id, distinct across ids."""
    assert _chain_lock("id-a") is _chain_lock("id-a")
    assert _chain_lock("id-a") is not _chain_lock("id-b")
