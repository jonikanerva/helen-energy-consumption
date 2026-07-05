# Helen Energy Consumption

<img src="https://raw.githubusercontent.com/jonikanerva/helen-energy-consumption/main/assets/icon.png" alt="Helen Energy Consumption" width="128" align="left">

A **minimal** Home Assistant custom component (HACS-compatible) that imports your
hourly electricity **consumption** from [Oma Helen](https://www.helen.fi/) into
Home Assistant's statistics database, so it shows up in the **Energy Dashboard**.

This is intentionally stripped down: **consumption only**. No cost tracking, no
spot prices, no contract-type logic, no extra sensors.

## How it works

The heavy lifting — authenticating against Oma Helen and fetching measurement
data — is done by the [`oma-helen-cli`](https://pypi.org/project/oma-helen-cli/)
Python library, which Home Assistant installs automatically (declared in
`manifest.json`). This component is a thin wrapper:

1. Logs in with your Oma Helen credentials.
2. Every 3 hours, fetches the last 7 days of hourly consumption (plus a day
   of slack for Helen's 1–2 day publication lag).
3. Writes it as a cumulative external statistic (`kWh`, `has_sum`) that the
   Energy Dashboard understands. Late-arriving hours are zero-filled and then
   repaired once real data appears, keeping the cumulative chain correct.

No entities are created — the integration runs purely in the background.

## Installation (HACS custom repository)

1. In HACS → _Integrations_ → ⋮ → _Custom repositories_, add this repository's
   URL with category **Integration**.
2. Install **Helen Energy Consumption** and restart Home Assistant.
3. _Settings → Devices & Services → Add Integration →_ **Helen Energy
   Consumption**, and sign in with your Oma Helen credentials.
4. If your account has more than one meter, you'll be asked to pick a delivery
   site.

## Adding to the Energy Dashboard

_Settings → Dashboards → Energy → Electricity grid → Add consumption_, then pick
the **Helen Consumption** statistic. Data may take a poll cycle (and Helen's own
1–2 day measurement lag) to fill in.

## Backfill and diagnostics

Two technical surfaces exist for maintenance; day-to-day use needs neither:

- **Backfill** — _Developer Tools → Actions →_ **Backfill consumption
  history** rebuilds the statistics chain for a delivery site from a chosen
  start date through now. Use it to import older history right after setup,
  or to repair a broken range.
- **Diagnostics** — _Settings → Devices & Services → Helen Energy
  Consumption → Download diagnostics_ produces a redacted snapshot
  (credentials and delivery-site id removed) for debugging a stalled import.

## Files

```
custom_components/helen_energy_consumption/
├── __init__.py       # setup + 3-hour poll timer + backfill action
├── manifest.json     # metadata + oma-helen-cli dependency
├── const.py          # constants
├── config_flow.py    # credential / site setup UI
├── coordinator.py    # login + drive imports
├── statistics.py     # fetch consumption, write external statistic
├── diagnostics.py    # redacted diagnostics download
├── services.yaml     # backfill action schema
└── translations/en.json
```

## License

MIT — see [LICENSE](LICENSE).
