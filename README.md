# Helen Energy Consumption

A **minimal** Home Assistant custom component (HACS-compatible) that imports your
hourly electricity **consumption** from [Oma Helen](https://www.helen.fi/) into
Home Assistant's statistics database, so it shows up in the **Energy Dashboard**.

This is intentionally stripped down: **consumption only**. No cost tracking, no
spot prices, no contract-type logic, no extra sensors. If you want prices and
cost sensors, use the full
[oma-helen-ha-integration](https://github.com/carohauta/oma-helen-ha-integration).

## How it works

The heavy lifting — authenticating against Oma Helen and fetching measurement
data — is done by the [`oma-helen-cli`](https://pypi.org/project/oma-helen-cli/)
Python library, which Home Assistant installs automatically (declared in
`manifest.json`). This component is a thin wrapper:

1. Logs in with your Oma Helen credentials.
2. Every 3 hours, fetches the last 7 days of hourly consumption.
3. Writes it as a cumulative external statistic (`kWh`, `has_sum`) that the
   Energy Dashboard understands. Late-arriving hours are zero-filled and then
   repaired once real data appears, keeping the cumulative chain correct.

No entities are created — the integration runs purely in the background.

## Installation (HACS custom repository)

1. In HACS → *Integrations* → ⋮ → *Custom repositories*, add this repository's
   URL with category **Integration**.
2. Install **Helen Energy Consumption** and restart Home Assistant.
3. *Settings → Devices & Services → Add Integration →* **Helen Energy
   Consumption**, and sign in with your Oma Helen credentials.
4. If your account has more than one meter, you'll be asked to pick a delivery
   site.

## Adding to the Energy Dashboard

*Settings → Dashboards → Energy → Electricity grid → Add consumption*, then pick
the **Helen Consumption** statistic. Data may take a poll cycle (and Helen's own
1–2 day measurement lag) to fill in.

## Files

```
custom_components/helen_energy_consumption/
├── __init__.py       # setup + 3-hour poll timer
├── manifest.json     # metadata + oma-helen-cli dependency
├── const.py          # constants
├── config_flow.py    # credential / site setup UI
├── coordinator.py    # login + drive imports
├── statistics.py     # fetch consumption, write external statistic
└── translations/en.json
```
