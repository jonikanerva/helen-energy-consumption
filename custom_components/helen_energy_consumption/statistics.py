"""Statistics manager for the Helen Energy Consumption integration.

Fetches hourly electricity consumption from Helen and writes it as a single
cumulative external statistic (kWh, has_sum) that Home Assistant's Energy
Dashboard can consume directly. Costs and spot prices are intentionally out of
scope — this integration only imports consumption.
"""

from __future__ import annotations

import logging
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

from .const import DOMAIN, STATISTICS_BACKFILL_HOURS

_LOGGER = logging.getLogger(__name__)


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
        config_entry_id: str,
        name: str,
    ) -> None:
        """Initialize the statistics manager.

        Args:
            hass: Home Assistant instance.
            api_client: Authenticated Helen API client.
            config_entry_id: Config entry ID, used to build a unique statistic_id.
            name: Human-readable name for the statistic (shown in the dashboard).
        """
        self.hass = hass
        self.api_client = api_client
        self.name = name

        # statistic_ids only allow lowercase, digits and underscores.
        suffix = config_entry_id.replace("-", "").lower()[:8]
        self.consumption_statistic_id = f"{DOMAIN}:hourly_energy_consumption_{suffix}"

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

        api_entries: dict[datetime, MeasurementsWithSpotPriceSeries] = {}
        for entry in series:
            hour = self._convert_to_utc(entry.start).replace(
                minute=0, second=0, microsecond=0
            )
            api_entries[hour] = entry

        if not api_entries:
            return

        earliest_api = min(api_entries.keys())

        real_hours = [
            h for h, e in api_entries.items() if e.electricity is not None
        ]
        if not real_hours:
            _LOGGER.debug("No hours with real consumption data yet, skipping")
            return
        latest_real_hour = max(real_hours)

        now_utc = datetime.now(ZoneInfo("UTC")).replace(
            minute=0, second=0, microsecond=0
        )
        window_end = now_utc + timedelta(hours=1)
        existing = await self._get_existing_statistics_in_window(
            self.consumption_statistic_id, earliest_api, window_end
        )

        # Repair previously zero-filled hours that now have real data.
        await self._repair_zero_filled_hours(api_entries, existing)

        if existing:
            last_db_hour = max(existing.keys())
            cumulative = existing[last_db_hour]
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
    ) -> None:
        """Upgrade previously zero-filled hours that now have real API data.

        A zero-filled hour leaves the cumulative sum unchanged from the previous
        hour. When real data later arrives, apply the delta via
        async_adjust_statistics so HA cascades it to all later records.
        """
        sorted_hours = sorted(existing.keys())
        if len(sorted_hours) < 2:
            return

        recorder = get_instance(self.hass)
        repaired = 0
        for prev_hour, curr_hour in zip(sorted_hours, sorted_hours[1:]):
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

    async def _get_existing_statistics_in_window(
        self, statistic_id: str, start_time: datetime, end_time: datetime
    ) -> dict[datetime, float]:
        """Return {hour: cumulative sum} for existing records in the window."""
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
        except Exception as err:  # noqa: BLE001 - query is best-effort
            _LOGGER.warning("Error querying existing statistics: %s", err)
            return {}

        existing: dict[datetime, float] = {}
        for stat in stats.get(statistic_id, []):
            raw = stat["start"]
            if isinstance(raw, datetime):
                ts = (
                    raw.replace(tzinfo=ZoneInfo("UTC"))
                    if raw.tzinfo is None
                    else raw.astimezone(ZoneInfo("UTC"))
                )
            else:
                ts = datetime.fromtimestamp(raw, tz=ZoneInfo("UTC"))
            ts = ts.replace(minute=0, second=0, microsecond=0)
            existing[ts] = stat.get("sum", 0.0)
        return existing

    def _convert_to_utc(self, helsinki_timestamp: str) -> datetime:
        """Convert an ISO 8601 (Helsinki) timestamp string to UTC."""
        return datetime.fromisoformat(helsinki_timestamp).astimezone(ZoneInfo("UTC"))

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
