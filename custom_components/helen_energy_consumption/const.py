"""Constants for the Helen Energy Consumption integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "helen_energy_consumption"

CONF_DELIVERY_SITE_ID: Final = "delivery_site_id"

# The backfill admin action (Developer Tools -> Actions) and its fields.
SERVICE_BACKFILL: Final = "backfill"
ATTR_START_DATE: Final = "start_date"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

# How often the integration re-fetches hourly consumption from Helen.
SCAN_INTERVAL: Final = timedelta(hours=3)

# How far back each poll re-fetches hourly data. Helen publishes measurements
# with a lag of a day or two, so a rolling window lets late-arriving hours be
# repaired. Hours older than this window stay zero-filled if they were ever
# missing.
ROLLING_WINDOW_HOURS: Final = 168  # 7 days
