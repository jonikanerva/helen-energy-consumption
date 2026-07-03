"""Statistics manager for the Helen Energy Consumption integration.

Fetches hourly electricity consumption from Helen and writes it as a single
cumulative external statistic (kWh, has_sum) that Home Assistant's Energy
Dashboard can consume directly. Costs and spot prices are intentionally out of
scope — this integration only imports consumption.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helenservice import RESOLUTION_HOUR, HelenApiClient
from helenservice.api_exceptions import InvalidApiResponseException
from helenservice.api_response import (
    MeasurementsWithSpotPriceResponse,
    MeasurementsWithSpotPriceSeries,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

# StatisticMeanType is only available in newer HA cores.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

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

from .const import DOMAIN, STATISTICS_BACKFILL_HOURS

_LOGGER = logging.getLogger(__name__)


class StatisticsQueryError(Exception):
    """Raised when the recorder query for existing statistics fails.

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
            STATISTICS_BACKFILL_HOURS,
        )

    async def import_recent_statistics(self) -> None:
        """Extend the statistics chain with the latest hourly data from the API."""
        series = await self._fetch_interval_data()
        await self._write_statistics_chain(series)

    async def _fetch_interval_data(self) -> list[MeasurementsWithSpotPriceSeries]:
        """Fetch hourly consumption for the rolling backfill window."""
        end_date = date.today()
        start_date = end_date - timedelta(days=STATISTICS_BACKFILL_HOURS // 24 + 1)

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

        _LOGGER.debug("Received %d hourly intervals", len(response.series))
        if response.missing_series:
            _LOGGER.debug(
                "API reported %d missing hourly intervals", len(response.missing_series)
            )
        return response.series

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

        api_entries: dict[datetime, MeasurementsWithSpotPriceSeries] = {}
        for entry in series:
            hour = self._convert_to_utc(entry.start).replace(
                minute=0, second=0, microsecond=0
            )
            api_entries[hour] = entry

        if not api_entries:
            return

        earliest_api = min(api_entries.keys())

        real_hours = [h for h, e in api_entries.items() if e.electricity is not None]
        if not real_hours:
            _LOGGER.debug("No hours with real consumption data yet, skipping")
            return
        latest_real_hour = max(real_hours)

        now_utc = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        window_end = now_utc + timedelta(hours=1)
        try:
            existing = await self._get_existing_statistics_in_window(
                self.consumption_statistic_id, earliest_api, window_end
            )
        except StatisticsQueryError:
            # History could not be read. Skip this poll; a later one retries.
            return

        # Repair previously zero-filled hours that now have real data. The
        # repair cascades its positive delta forward through the DB, including
        # last_db_hour, so anchor the forward walk on the repaired total to
        # avoid dropping energy at the boundary.
        repaired_delta = await self._repair_zero_filled_hours(api_entries, existing)

        if existing:
            last_db_hour = max(existing.keys())
            cumulative = existing[last_db_hour] + repaired_delta
            walk_start = last_db_hour + timedelta(hours=1)
        else:
            cumulative = 0.0
            walk_start = earliest_api

        if walk_start > latest_real_hour:
            _LOGGER.debug(
                "Up to date: DB at %s, latest real API hour %s",
                walk_start.isoformat(),
                latest_real_hour.isoformat(),
            )
            return

        stats: list[StatisticData] = []
        zero_filled = 0
        current_hour = walk_start
        while current_hour <= latest_real_hour:
            entry = api_entries.get(current_hour)
            electricity = entry.electricity if entry else None
            if electricity is None:
                # No data yet — hold the cumulative sum flat. The repair pass
                # upgrades this hour once real data arrives.
                electricity = 0.0
                zero_filled += 1

            cumulative += electricity
            stats.append(
                StatisticData(
                    start=current_hour,
                    state=_safe_round(cumulative),
                    sum=_safe_round(cumulative),
                )
            )
            current_hour += timedelta(hours=1)

        if not stats:
            return

        await self._import_statistics(stats)
        _LOGGER.info(
            "Wrote %d hour(s) for %s (zero_filled=%d)",
            len(stats),
            self.consumption_statistic_id,
            zero_filled,
        )

    async def _repair_zero_filled_hours(
        self,
        api_entries: dict[datetime, MeasurementsWithSpotPriceSeries],
        existing: dict[datetime, float],
    ) -> float:
        """Upgrade previously zero-filled hours that now have real API data.

        A zero-filled hour leaves the cumulative sum unchanged from the previous
        hour. When real data later arrives, apply the delta via
        async_adjust_statistics so HA cascades it to all later records. Returns
        the total repaired delta, which the caller adds to the anchor so the
        forward walk starts from the post-repair cumulative value.
        """
        sorted_hours = sorted(existing.keys())
        if len(sorted_hours) < 2:
            return 0.0

        recorder = get_instance(self.hass)
        repaired = 0
        repaired_delta = 0.0
        for prev_hour, curr_hour in zip(sorted_hours, sorted_hours[1:], strict=False):
            if curr_hour != prev_hour + timedelta(hours=1):
                continue
            if existing[curr_hour] - existing[prev_hour] != 0.0:
                continue  # cumulative moved — hour already has real data

            entry = api_entries.get(curr_hour)
            if entry is None or entry.electricity is None or entry.electricity == 0.0:
                continue

            recorder.async_adjust_statistics(
                self.consumption_statistic_id, curr_hour, entry.electricity, "kWh"
            )
            repaired += 1
            repaired_delta += entry.electricity
            _LOGGER.debug(
                "Repaired zero-filled hour %s: +%.3f kWh",
                curr_hour.isoformat(),
                entry.electricity,
            )

        if repaired:
            _LOGGER.info(
                "Repaired %d zero-filled hour(s) for %s",
                repaired,
                self.consumption_statistic_id,
            )

        return repaired_delta

    async def _get_existing_statistics_in_window(
        self, statistic_id: str, start_time: datetime, end_time: datetime
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
            raw = stat["start"]
            if isinstance(raw, datetime):
                ts = (
                    raw.replace(tzinfo=dt_util.UTC)
                    if raw.tzinfo is None
                    else raw.astimezone(dt_util.UTC)
                )
            else:
                ts = dt_util.utc_from_timestamp(raw)
            ts = ts.replace(minute=0, second=0, microsecond=0)
            existing[ts] = stat.get("sum", 0.0)
        return existing

    async def _ensure_helsinki_tz(self) -> None:
        """Resolve the Europe/Helsinki zone once, off the event loop.

        Resolving a ZoneInfo does blocking tzdata filesystem I/O on first use,
        so it must never run on the loop; dt_util.async_get_time_zone does it in
        the executor. Only naive Helen timestamps consume this zone.
        """
        if self._helsinki_tz is None:
            self._helsinki_tz = await dt_util.async_get_time_zone("Europe/Helsinki")

    def _convert_to_utc(self, timestamp: str) -> datetime:
        """Convert a Helen ISO 8601 timestamp string to UTC.

        Helen normally returns offset-aware timestamps; a naive value is
        localized to Europe/Helsinki (never the host tz) before converting.
        """
        parsed = dt_util.parse_datetime(timestamp, raise_on_error=True)
        if parsed.tzinfo is None:
            # Naive input: localize to Helsinki. A DST fall-back duplicated
            # local hour collapses via fold=0 and is unrecoverable without an
            # offset — only relevant if Helen ever emits naive timestamps.
            parsed = parsed.replace(tzinfo=self._helsinki_tz)
        return parsed.astimezone(dt_util.UTC)

    async def _import_statistics(self, statistics: list[StatisticData]) -> None:
        """Write the cumulative consumption stream into the HA database."""
        metadata_kwargs = {
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

        metadata = StatisticMetaData(**metadata_kwargs)
        async_add_external_statistics(self.hass, metadata, statistics)
