# STACK

The concrete technical rules for this repository. `VISION.md` says _what_ we
build and why; this says _how_ and _with what_. Where the two conflict on
product scope, `VISION.md` wins; on technical mechanics, this file wins.

## Language & Runtime

- **Language:** Python — **user-owned**, do not change without explicit approval.
- **Minimum Python:** 3.12 (`requires-python = ">=3.12"`) — **user-owned**.
- **Target platform:** Home Assistant custom component, tested against HA Core
  **2025.1** (pinned via `pytest-homeassistant-custom-component`). Minimum HA is
  declared in `hacs.json` (`2025.1.0`).
- **Type strictness:** type hints on all public functions; `from __future__ import
annotations` in every module. Full static-type enforcement mode is **user-owned**.

## Frameworks

- **Home Assistant** integration APIs: config entries, config flow,
  `recorder`/statistics (`async_add_external_statistics`,
  `statistics_during_period`, `async_adjust_statistics`),
  `homeassistant.helpers.event.async_track_time_interval`.
- **`oma-helen-cli`** (`helenservice`) for all Helen API access — the only
  domain dependency. Pinned exactly in `manifest.json` `requirements`.
- **voluptuous** for config-flow schemas (bundled with HA).

## Build & verify commands

- Setup: `mise install` — installs the pinned tools (Python 3.12, uv) and runs
  `uv sync` via a postinstall hook, bringing the whole dev environment up in one
  command. Plain `uv sync` also works once the tools are present.
- **`$VERIFY_CMD`** (the single gate every change must pass):
  `uv run ruff check custom_components && uv run pytest tests/`
- Lint only: `uv run ruff check custom_components`
- Tests only: `uv run pytest tests/ -v`
- HA integration validation (run before release): `hassfest`

> The test/lint harness lives in `pyproject.toml` (dev dependency group, ruff and
> pytest config) and `tests/`. Home Assistant is pinned to the 2025.1 series via
> the dev group; the resolver allows pre-releases (`tool.uv.prerelease`) because
> HA depends on beta packages. `uv.lock` is committed for reproducible installs.

## Performance budgets

- All Helen API and recorder calls run in the executor
  (`hass.async_add_executor_job`) — **never block the event loop**.
- One poll fetches at most a 7-day hourly window (`STATISTICS_BACKFILL_HOURS`);
  do not widen the default rolling window without a documented reason.
- Default poll interval: **3 hours**. Do not poll more often than hourly —
  Helen's data lags 1–2 days, so faster polling buys nothing.
- Setup (`async_setup_entry`) does exactly one import; everything else is on the
  timer.

## Persistence shape

- **Config entry data:** `username`, `password`, optional `delivery_site_id`.
  Nothing else is persisted in the entry.
- **Statistics:** one external statistic per config entry,
  `statistic_id = helen_energy_consumption:hourly_energy_consumption_<8-char suffix>`,
  unit `kWh`, `has_sum = True`, cumulative `state`/`sum`.
- The cumulative chain is anchored to the last DB record in the query window;
  gaps are zero-filled and later repaired via `async_adjust_statistics`. Never
  rewrite history outside the current rolling window.

## Approved dependencies

- `oma-helen-cli` (pinned) — Helen API access.
- Anything already shipped with Home Assistant core (voluptuous, aiohttp, the
  recorder, helpers).
- Dev-only: `pytest`, `pytest-homeassistant-custom-component`, `ruff`.
- **Adding any other runtime dependency requires explicit approval** and a note
  here. Default answer is no — prefer stdlib or HA core helpers.

## Stack-specific reject-list additions

- ❌ Blocking I/O in the event loop (any Helen/recorder call not in the executor).
- ❌ Re-implementing Helen authentication or endpoint parsing — belongs in
  `oma-helen-cli`; push fixes upstream.
- ❌ Cost/price/spot/VAT/contract-type logic — out of scope per `VISION.md`.
- ❌ Writing statistics outside the current rolling window, or any code path
  that can erase/corrupt already-imported history on a transient failure.
- ❌ Adding entities, services, or Lovelace resources without a `VISION.md`
  decision-filter pass.
- ❌ Unpinned or floating dependency versions in `manifest.json`.
- ❌ Logging credentials or full API responses at INFO or above.
