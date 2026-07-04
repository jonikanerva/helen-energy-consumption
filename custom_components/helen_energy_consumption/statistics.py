"""Statistics manager for the Helen Energy Consumption integration.

Fetches hourly electricity consumption from Helen and writes it as a single
cumulative external statistic (kWh, has_sum) that Home Assistant's Energy
Dashboard can consume directly. Costs and spot prices are intentionally out of
scope — this integration only imports consumption.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import InvalidApiResponseException
from helenservice.api_response import (
    MeasurementsWithSpotPriceResponse,
    MeasurementsWithSpotPriceSeries,
)
from helenservice.const import RESOLUTION_HOUR
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

# Forward-compat: StatisticMeanType is the metadata API on HA > 2025.1;
# has_mean=False is the 2025.1 fallback.
try:
    # reason: StatisticMeanType was added after HA 2025.1; the ImportError
    # branch below is the 2025.1 path, so the attribute genuinely may not
    # exist on the pinned dev environment.
    from homeassistant.components.recorder.models import (  # type: ignore[attr-defined]
        StatisticMeanType,
    )

    HAS_MEAN_TYPE = True
except ImportError:
    HAS_MEAN_TYPE = False
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ROLLING_WINDOW_HOURS

_LOGGER = logging.getLogger(__name__)

# Lower bound for the strict pre-start anchor read (statistics_during_period
# requires a start_time; the Unix epoch is safely before any Helen data).
_ANCHOR_READ_START = dt_util.utc_from_timestamp(0)

# Chain writes are serialized per statistic_id, keyed independently of any
# coordinator instance. On a config-entry reload the old and new coordinators
# hold different per-instance locks (issue #7) but write the SAME chain, so an
# in-flight old writer could interleave with the new one. This shared lock makes
# all writers of a chain contend the same lock (issue #18). Created lazily on
# the event loop; the poll's per-instance lock stays the OUTER lock (order is
# always instance -> chain, never reversed).
_CHAIN_LOCKS: dict[str, asyncio.Lock] = {}


def _chain_lock(statistic_id: str) -> asyncio.Lock:
    """Return the shared write lock for a statistic_id (created on the loop)."""
    return _CHAIN_LOCKS.setdefault(statistic_id, asyncio.Lock())


class StatisticsQueryError(Exception):
    """Raised when the recorder query fails or a row's sum is unreadable.

    Signals the caller to skip this poll rather than treat the error as an
    empty database and rewrite history from zero (VISION principle 5).
    """


def build_statistic_id(delivery_site_id: str) -> str:
    """Build the external statistic_id for a Helen delivery-site id.

    External statistic object ids allow only lowercase letters, digits and
    underscores, so any other character in the delivery-site id is replaced.
    """
    object_id = re.sub(r"[^a-z0-9_]", "_", str(delivery_site_id).lower())
    return f"{DOMAIN}:consumption_{object_id}"


def _safe_round(value: float | None, decimals: int = 3) -> float:
    """Round a value, returning 0.0 if it is None or non-numeric."""
    if value is None:
        return 0.0
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return 0.0


def _floor_hour(dt: datetime) -> datetime:
    """Floor a datetime to the top of its hour, preserving tzinfo and fold."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _row_start_to_utc(raw: float | datetime) -> datetime:
    """Normalize a recorder row start (epoch float or datetime) to aware UTC."""
    if isinstance(raw, datetime):
        return (
            raw.replace(tzinfo=dt_util.UTC)
            if raw.tzinfo is None
            else raw.astimezone(dt_util.UTC)
        )
    return dt_util.utc_from_timestamp(raw)


def _accumulate_row(
    cumulative: float, elec: float, hour: datetime
) -> tuple[float, StatisticData]:
    """Add one hour's energy to the running cumulative and emit its chain row.

    Returns the UNROUNDED cumulative (the running carry) and a StatisticData
    whose ``state`` and ``sum`` share one ``_safe_round(cumulative)`` value. No
    clamping happens here — each caller resolves/clamps ``elec`` beforehand as
    its own algorithm requires.
    """
    cumulative += elec
    rounded = _safe_round(cumulative)
    return cumulative, StatisticData(start=hour, state=rounded, sum=rounded)


class HelenConsumptionStatistics:
    """Import Helen hourly consumption into the HA statistics database."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: HelenApiClient,
        delivery_site_id: str,
        name: str,
    ) -> None:
        """Initialize the statistics manager.

        Args:
            hass: Home Assistant instance.
            api_client: Authenticated Helen API client.
            delivery_site_id: Helen delivery-site id the statistic is keyed on.
            name: Human-readable name for the statistic (shown in the dashboard).
        """
        self.hass = hass
        self.api_client = api_client
        self.name = name
        # Resolved once off the event loop; only the naive-timestamp input edge
        # needs it (see _ensure_helsinki_tz / _convert_to_utc).
        self._helsinki_tz: ZoneInfo | None = None

        self.consumption_statistic_id = build_statistic_id(delivery_site_id)

        _LOGGER.debug(
            "Initialized consumption statistics for %s (%d hour window)",
            self.consumption_statistic_id,
            ROLLING_WINDOW_HOURS,
        )

    async def import_recent_statistics(self) -> None:
        """Extend the statistics chain with the latest hourly data from the API."""
        series = await self._fetch_interval_data()
        await self._write_statistics_chain(series)

    async def _fetch_interval_data(self) -> list[MeasurementsWithSpotPriceSeries]:
        """Fetch hourly consumption for the rolling backfill window."""
        end_date = date.today()
        start_date = end_date - timedelta(days=ROLLING_WINDOW_HOURS // 24 + 1)

        # Clamp to contract start so new contracts don't 403 on the pre-contract
        # part of the window.
        try:
            contract_start = await self.hass.async_add_executor_job(
                self.api_client.get_contract_start_date
            )
            if contract_start is not None and contract_start > end_date:
                _LOGGER.debug(
                    "Fetch window (%s to %s) is entirely before contract start %s",
                    start_date,
                    end_date,
                    contract_start,
                )
                return []
        except Exception as err:  # noqa: BLE001 - contract start is best-effort
            _LOGGER.debug("Could not read contract start date: %s", err)

        return await self._fetch_range_data(start_date, end_date)

    async def _fetch_range_data(
        self, start_date: date, end_date: date
    ) -> list[MeasurementsWithSpotPriceSeries]:
        """Fetch hourly consumption for [start_date, end_date] in the executor."""
        _LOGGER.debug("Fetching hourly consumption from %s to %s", start_date, end_date)

        try:
            response: MeasurementsWithSpotPriceResponse = (
                await self.hass.async_add_executor_job(
                    self.api_client.get_measurements_with_spot_prices,
                    start_date,
                    end_date,
                    RESOLUTION_HOUR,
                )
            )
        except InvalidApiResponseException as err:
            if "no-relevant-contract" in str(err).lower():
                _LOGGER.warning(
                    "No consumption data for %s to %s (outside contract period)",
                    start_date,
                    end_date,
                )
                return []
            raise

        series: list[MeasurementsWithSpotPriceSeries] = response.series
        _LOGGER.debug("Received %d hourly intervals", len(series))
        if response.missing_series:
            _LOGGER.debug(
                "API reported %d missing hourly intervals", len(response.missing_series)
            )
        return series

    async def _write_statistics_chain(
        self, series: list[MeasurementsWithSpotPriceSeries]
    ) -> None:
        """Extend the cumulative consumption chain from the given hourly series.

        Anchors to the last record already in the DB window, repairs any
        previously zero-filled hours that now have real data, then walks forward
        to the latest real hour, zero-filling gaps so the cumulative sum stays
        flat until real data arrives.
        """
        if not series:
            _LOGGER.debug("No interval data to process")
            return

        # Resolve the Helsinki zone off the loop before the (sync) conversion
        # loop, so _convert_to_utc never blocks on tzdata I/O.
        await self._ensure_helsinki_tz()

        api_elec = self._bucket_series_by_utc_hour(series)
        if not api_elec:
            return

        earliest_api = min(api_elec.keys())

        real_hours = [h for h, elec in api_elec.items() if elec is not None]
        if not real_hours:
            _LOGGER.debug("No hours with real consumption data yet, skipping")
            return
        latest_real_hour = max(real_hours)

        now_utc = _floor_hour(dt_util.utcnow())
        window_end = now_utc + timedelta(hours=1)

        # Serialize the whole read -> repair -> write against any other writer of
        # this same chain (e.g. an old and a new coordinator across a reload).
        # See issue #18. The in-memory prelude above stays outside the lock.
        async with _chain_lock(self.consumption_statistic_id):
            try:
                existing = await self._get_existing_statistics_in_window(
                    self.consumption_statistic_id, earliest_api, window_end
                )
            except StatisticsQueryError:
                # History could not be read. Skip this poll; a later one retries.
                return

            # Repair previously zero-filled hours that now have real data. The
            # repair cascades its positive delta forward through the DB,
            # including last_db_hour, so anchor the forward walk on the repaired
            # total to avoid dropping energy at the boundary.
            repairs = await self._repair_zero_filled_hours(api_elec, existing)
            repaired_delta = sum(delta for _, delta in repairs)

            # Flat-fill fully-absent interior hours (gaps strictly between
            # present rows). These carry post-repair sums (see
            # _fill_missing_interior_hours) and sit below last_db_hour with zero
            # delta, so they never touch the anchor; their real data lands on the
            # next poll's repair pass.
            missing_rows = self._fill_missing_interior_hours(existing, repairs)

            if existing:
                last_db_hour = max(existing.keys())
                cumulative = existing[last_db_hour] + repaired_delta
                walk_start = last_db_hour + timedelta(hours=1)
            else:
                cumulative = 0.0
                walk_start = earliest_api

            stats: list[StatisticData] = []
            zero_filled = 0
            if walk_start > latest_real_hour:
                _LOGGER.debug(
                    "Chain tail up to date: DB at %s, latest real API hour %s",
                    walk_start.isoformat(),
                    latest_real_hour.isoformat(),
                )
            else:
                current_hour = walk_start
                while current_hour <= latest_real_hour:
                    electricity = api_elec.get(current_hour)
                    if electricity is None:
                        # No data yet — hold the cumulative sum flat. The repair
                        # pass upgrades this hour once real data arrives.
                        electricity = 0.0
                        zero_filled += 1

                    cumulative, row = _accumulate_row(
                        cumulative, electricity, current_hour
                    )
                    stats.append(row)
                    current_hour += timedelta(hours=1)

            # missing_rows (< last_db_hour) and stats (>= last_db_hour + 1h) are
            # disjoint and each ascending, so the concatenation is ordered.
            combined = missing_rows + stats
            if combined:
                await self._import_statistics(combined)
                _LOGGER.debug(
                    "Wrote %d hour(s) for %s (interior_gap=%d, zero_filled=%d)",
                    len(combined),
                    self.consumption_statistic_id,
                    len(missing_rows),
                    zero_filled,
                )

            # GUARD #2: a repair enqueues a non-idempotent `sum += adj` on the
            # recorder thread, but reads run on a separate db_executor pool and
            # are NOT serialized with that write. Block until the recorder has
            # applied the queued adjustment before releasing the chain lock, so
            # the next holder reads post-repair sums and cannot re-detect and
            # re-apply the same repair (a permanent double-count across a
            # reload). Only repairs need this; the absolute-sum import is
            # idempotent under a stale read, so a no-repair poll never flushes.
            if repairs:
                await get_instance(self.hass).async_block_till_done()

    async def _repair_zero_filled_hours(
        self,
        api_elec: dict[datetime, float | None],
        existing: dict[datetime, float],
    ) -> list[tuple[datetime, float]]:
        """Upgrade previously zero-filled hours that now have real API data.

        A zero-filled hour leaves the cumulative sum unchanged from the previous
        hour. When real data later arrives, apply the delta via
        async_adjust_statistics so HA cascades it to all later records. Returns
        the (hour, delta) adjustments applied this poll; the caller derives the
        total to anchor the forward walk and computes post-repair sums for
        interior gap-fill from these deltas in memory (a DB re-read cannot be
        trusted — adjustments are queued on the recorder thread and may not be
        flushed before a read runs).
        """
        sorted_hours = sorted(existing.keys())
        if len(sorted_hours) < 2:
            return []

        recorder = get_instance(self.hass)
        repairs: list[tuple[datetime, float]] = []
        for prev_hour, curr_hour in zip(sorted_hours, sorted_hours[1:], strict=False):
            if curr_hour != prev_hour + timedelta(hours=1):
                continue
            if existing[curr_hour] - existing[prev_hour] != 0.0:
                continue  # cumulative moved — hour already has real data

            elec = api_elec.get(curr_hour)
            # Round-aware zero check: the library does no rounding and Helen's
            # precision is unverifiable, so a sub-0.0005 value must not be
            # re-detected as zero-filled and re-adjusted (double-count). A real
            # >= 0.001 kWh hour still repairs.
            if elec is None or _safe_round(elec) == 0.0:
                continue

            recorder.async_adjust_statistics(
                self.consumption_statistic_id, curr_hour, elec, "kWh"
            )
            repairs.append((curr_hour, elec))
            _LOGGER.debug(
                "Repaired zero-filled hour %s: +%.3f kWh",
                curr_hour.isoformat(),
                elec,
            )

        if repairs:
            _LOGGER.debug(
                "Repaired %d zero-filled hour(s) for %s",
                len(repairs),
                self.consumption_statistic_id,
            )

        return repairs

    def _fill_missing_interior_hours(
        self,
        existing: dict[datetime, float],
        repairs: list[tuple[datetime, float]],
    ) -> list[StatisticData]:
        """Insert flat rows for fully-absent interior hours in the window.

        For each consecutive present pair with a gap, every absent hour strictly
        between them gets a flat row carrying the post-repair cumulative sum at
        ``prev_hour``. That sum is computed in memory as ``existing[prev_hour]``
        plus every repair delta applied at an hour <= ``prev_hour`` — the same
        value the cascading adjustments leave in the DB — so the chain stays
        monotonic without a re-read racing the queued adjustments. Existing rows
        are never modified; the real data for these hours is applied on the next
        poll by the adjacent-pair repair path.
        """
        sorted_hours = sorted(existing.keys())
        if len(sorted_hours) < 2:
            return []

        rows: list[StatisticData] = []
        for prev_hour, curr_hour in zip(sorted_hours, sorted_hours[1:], strict=False):
            if curr_hour <= prev_hour + timedelta(hours=1):
                continue  # adjacent — no interior gap

            flat_sum = _safe_round(
                existing[prev_hour]
                + sum(delta for hour, delta in repairs if hour <= prev_hour)
            )
            gap_hour = prev_hour + timedelta(hours=1)
            while gap_hour < curr_hour:
                rows.append(StatisticData(start=gap_hour, state=flat_sum, sum=flat_sum))
                gap_hour += timedelta(hours=1)

        if rows:
            _LOGGER.debug(
                "Filling %d missing interior hour(s) for %s",
                len(rows),
                self.consumption_statistic_id,
            )
        return rows

    async def rebuild_range(self, start_date: date) -> None:
        """Rebuild the cumulative chain for [start_date, now] from Helen data.

        Re-derives each hour from the Helen API where available and preserves
        the existing DB contribution otherwise, then performs a SINGLE
        async_add_external_statistics write. Everything before that write is a
        read or an in-memory computation, so any failure (fetch, empty response,
        recorder read) raises before the write and leaves prior statistics fully
        intact (VISION principle 5). Rows before start_utc are never touched.

        Known limitation: if Helen later revises a past hour while omitting
        others in the range, a small correction may be attributed to a
        neighbouring hour; range totals stay monotonic.
        """
        await self._ensure_helsinki_tz()

        # Helsinki 00:00 of start_date, converted to the UTC hour bucket.
        start_utc = _floor_hour(
            self._convert_to_utc(datetime.combine(start_date, time.min).isoformat())
        )

        # The slow Helen fetch and bucketing stay OUTSIDE the chain lock so they
        # never hold up a concurrent poll of the same chain.
        series = await self._fetch_range_data(start_date, date.today())
        if not series:
            # The caller asked for a range Helen has no data for: idiomatic
            # service-validation error (cf. the all-unparseable / tz cases,
            # which are systemic HomeAssistantError).
            raise ServiceValidationError(
                f"Helen returned no consumption data for "
                f"{self.consumption_statistic_id} from {start_date}"
            )

        api_elec = self._bucket_series_by_utc_hour(series)
        if not api_elec:
            # Data was returned but no hour parsed — fail closed (write nothing)
            # rather than treat it as an empty DB and rebuild from zero. (A few
            # bad hours among good ones proceed; this is the all-bad case.)
            raise HomeAssistantError(
                f"Helen returned data but no hour parsed for "
                f"{self.consumption_statistic_id}"
            )

        # Serialize both chain reads and the write against any other writer of
        # this chain (issue #18). rebuild_range uses only the idempotent
        # absolute-sum import, so no recorder flush is needed on release.
        async with _chain_lock(self.consumption_statistic_id):
            now_hour = _floor_hour(dt_util.utcnow())

            # end_time=None so max(in_range) is the TRUE DB tail (GUARD #2): the
            # rebuild covers every existing hour and is never truncated below it.
            in_range = await self._get_existing_statistics_in_window(
                self.consumption_statistic_id, start_utc, None
            )
            anchor_sum = await self._get_anchor_sum(start_utc)

            rebuild_end = max(now_hour, max(in_range)) if in_range else now_hour

            rows: list[StatisticData] = []
            cumulative = anchor_sum  # predecessor sum; the anchor for hour one
            current_hour = start_utc
            while current_hour <= rebuild_end:
                api_value = api_elec.get(current_hour)
                if api_value is not None:
                    elec = api_value
                elif current_hour in in_range:
                    # Preserve rule: keep this real hour's contribution measured
                    # against the RUNNING predecessor (cumulative), never an
                    # absent in_range[h-1]. A genuine zero-consumption hour gives
                    # delta 0 and harmlessly holds the sum flat.
                    elec = in_range[current_hour] - cumulative
                else:
                    elec = 0.0

                elec = max(elec, 0.0)  # Helen consumption is non-negative
                cumulative, row = _accumulate_row(cumulative, elec, current_hour)
                rows.append(row)
                current_hour += timedelta(hours=1)

            if not rows:
                return

            # Single, all-or-nothing write. Nothing was written before this line.
            await self._import_statistics(rows)
            _LOGGER.info(
                "Backfill rebuilt %d hour(s) for %s from %s",
                len(rows),
                self.consumption_statistic_id,
                start_utc.isoformat(),
            )

    async def _get_anchor_sum(self, start_utc: datetime) -> float:
        """Return the cumulative sum of the last row strictly before start_utc.

        Anchors the rebuild so it stays continuous with untouched history.
        Returns 0.0 when there is no prior row (onboarding). The read is strict
        (start < start_utc), matching the rebuild's inclusive start at start_utc
        so the anchor hour is never double-counted.
        """
        before = await self._get_existing_statistics_in_window(
            self.consumption_statistic_id, _ANCHOR_READ_START, start_utc
        )
        if not before:
            return 0.0
        return before[max(before)]

    async def _get_existing_statistics_in_window(
        self, statistic_id: str, start_time: datetime, end_time: datetime | None
    ) -> dict[datetime, float]:
        """Return {hour: cumulative sum} for existing records in the window.

        Raises StatisticsQueryError if the recorder query fails, so the caller
        can skip the poll instead of mistaking the error for an empty database.
        """
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                end_time,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
        except Exception as err:  # noqa: BLE001 - re-raised to abort the poll safely
            # A read failure must not be mistaken for an empty database: the
            # caller would rewrite the whole window from zero and corrupt real
            # history. Signal the failure so the poll is skipped instead.
            _LOGGER.warning(
                "Error querying existing statistics; skipping poll to preserve "
                "history: %s",
                err,
            )
            raise StatisticsQueryError(str(err)) from err

        existing: dict[datetime, float] = {}
        for stat in stats.get(statistic_id, []):
            ts = _floor_hour(_row_start_to_utc(stat["start"]))
            row_sum = stat.get("sum", 0.0)
            if row_sum is None:
                # A present-but-None sum for our has_sum stream can only mean
                # another writer or DB corruption — anchoring on 0.0 would
                # re-base the cumulative chain (a meter reset in the Energy
                # Dashboard). Fail the read instead; ruling in issue #32. A
                # row missing the key keeps the long-standing 0.0 default.
                _LOGGER.warning(
                    "Statistics row for %s at %s has no readable sum; "
                    "skipping poll to preserve history",
                    statistic_id,
                    ts.isoformat(),
                )
                raise StatisticsQueryError(
                    f"Statistics row for {statistic_id} at {ts.isoformat()} "
                    "has no readable sum"
                )
            existing[ts] = row_sum
        return existing

    async def _ensure_helsinki_tz(self) -> None:
        """Resolve the Europe/Helsinki zone once, off the event loop.

        Resolving a ZoneInfo does blocking tzdata filesystem I/O on first use,
        so it must never run on the loop; dt_util.async_get_time_zone does it in
        the executor. Only naive Helen timestamps consume this zone.
        """
        if self._helsinki_tz is None:
            self._helsinki_tz = await dt_util.async_get_time_zone("Europe/Helsinki")

    def _bucket_series_by_utc_hour(
        self, series: list[MeasurementsWithSpotPriceSeries]
    ) -> dict[datetime, float | None]:
        """Bucket a Helen series into {UTC hour: electricity} (last write wins).

        Requires _ensure_helsinki_tz() to have run (it uses _convert_to_utc).
        Kept synchronous and called before the #18 chain lock is taken — the
        same pre-read position as the inline builds it replaces.

        A single entry with an unparseable timestamp is skipped and logged
        rather than aborting the whole window; the hour becomes a gap the repair
        path fills, and a *persistently* unparseable hour simply stays
        zero-filled (accepted — the same class as an hour outside the rolling
        window). A systemic tz failure raises HomeAssistantError from
        _convert_to_utc and is deliberately NOT caught here (only ValueError /
        TypeError are), so it propagates fail-closed.
        """
        buckets: dict[datetime, float | None] = {}
        skipped = 0
        for entry in series:
            try:
                hour = _floor_hour(self._convert_to_utc(entry.start))
            except (ValueError, TypeError) as err:
                # Per-hour parse failure only — never the tz HomeAssistantError.
                skipped += 1
                _LOGGER.warning(
                    "Skipping unparseable hour %r for %s: %s",
                    entry.start,
                    self.consumption_statistic_id,
                    err,
                )
                continue
            buckets[hour] = entry.electricity
        if skipped:
            _LOGGER.warning(
                "Skipped %d unparseable hour(s) for %s",
                skipped,
                self.consumption_statistic_id,
            )
        return buckets

    def _convert_to_utc(self, timestamp: str) -> datetime:
        """Convert a Helen ISO 8601 timestamp string to UTC.

        Helen normally returns offset-aware timestamps; a naive value is
        localized to Europe/Helsinki (never the host tz) before converting.
        """
        parsed = dt_util.parse_datetime(timestamp, raise_on_error=True)
        if parsed.tzinfo is None:
            if self._helsinki_tz is None:
                # Fail closed rather than silently localize via the host tz
                # (the exact mis-bucketing #12 fixed). HomeAssistantError, not
                # ValueError/TypeError, so the bucketer's narrow except never
                # masks this systemic failure.
                raise HomeAssistantError(
                    "Europe/Helsinki time zone unavailable; refusing to "
                    "localize a naive Helen timestamp (would mis-bucket via "
                    "the host tz)"
                )
            # Naive input: localize to Helsinki. A DST fall-back duplicated
            # local hour collapses via fold=0 and is unrecoverable without an
            # offset — only relevant if Helen ever emits naive timestamps.
            parsed = parsed.replace(tzinfo=self._helsinki_tz)
        return parsed.astimezone(dt_util.UTC)

    async def _import_statistics(self, statistics: list[StatisticData]) -> None:
        """Write the cumulative consumption stream into the HA database."""
        # reason: carries forward-compat keys (unit_class, mean_type) absent
        # from HA 2025.1's StatisticMetaData.
        metadata_kwargs: dict[str, Any] = {
            "has_sum": True,
            "name": f"{self.name} - Consumption",
            "source": DOMAIN,
            "statistic_id": self.consumption_statistic_id,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "unit_class": "energy",
        }
        if HAS_MEAN_TYPE:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            metadata_kwargs["has_mean"] = False

        # reason: 2025.1's TypedDict predates mean_type; keys are version-gated
        # above and both branches are test-covered.
        metadata = StatisticMetaData(**metadata_kwargs)  # type: ignore[typeddict-item]
        async_add_external_statistics(self.hass, metadata, statistics)
