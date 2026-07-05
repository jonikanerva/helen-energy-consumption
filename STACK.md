# STACK.md — Helen Energy Consumption (Python 3.12 / Home Assistant custom integration, HACS)

> The concrete technical rules for this repository: a strict-typed, async Home Assistant **custom integration** distributed through HACS that imports hourly electricity consumption from Oma Helen into Home Assistant's long-term statistics. Correctness first — model impossible states as impossible, validate at the boundary, keep the event loop unblocked, add no dependency without justification — expressed through Home Assistant's own idioms rather than against the grain of the ecosystem.
>
> `VISION.md` says _what_ we build and why; this file says _how_ and _with what_. Where the two conflict on product scope, `VISION.md` wins; on technical mechanics, this file wins.
>
> **Normative.** `MUST`, `MUST NOT`, `SHOULD`, and `MAY` are binding as written. When this document conflicts with Home Assistant's own developer rules or the [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/), **Home Assistant wins** — surface the conflict before deviating. Deliberate departures from HA's standard patterns are recorded in §11, never left implicit.

---

## 0. Project shape

- **Shape:** Home Assistant custom integration (`custom_components/helen_energy_consumption/`), installed via HACS. `integration_type: service`, `iot_class: cloud_polling`. **No entities** — it imports hourly electricity consumption from Helen into Home Assistant's **statistics database** so it can be added to the Energy Dashboard.
- **Critical execution path:** the Home Assistant **asyncio event loop**. It is single-threaded and shared with the entire instance; blocking it degrades every integration and the UI. Every Helen and recorder call is blocking, so it MUST run via `hass.async_add_executor_job`; the loop MUST NOT block.
- **Applicable states:** there are no entities, so state is expressed as **statistics freshness** and **config-entry state** (`LOADED` / `SETUP_RETRY` / reauth), not entity availability. Setup failure → `ConfigEntryNotReady` (retry with backoff) or `ConfigEntryAuthFailed` (reauth); the timer poll is fail-quiet and never crashes the integration or touches existing history.
- **Module layout:**

```txt
custom_components/helen_energy_consumption/
  __init__.py          # async_setup (backfill admin action) + async_setup_entry/unload; wiring only
  manifest.json        # domain, requirements (oma-helen-cli==1.8.0, pinned), iot_class
  const.py             # DOMAIN, SCAN_INTERVAL, ROLLING_WINDOW_HOURS, config/service keys — no logic
  config_flow.py       # boundary: username/password/delivery_site_id, voluptuous-validated
  coordinator.py       # HelenConsumptionCoordinator — thin, timer-driven (see §11)
  statistics.py        # HelenConsumptionStatistics — recorder external-statistics import/repair
  diagnostics.py       # redacted config-entry diagnostics (§8 TO_REDACT)
  services.yaml        # backfill admin action schema (UI)
  translations/*.json  # en, fi
tests/                 # pytest + pytest-homeassistant-custom-component
.mise.toml             # pinned tool/runtime versions (Python 3.12, uv) + verify task + uv sync hook
pyproject.toml         # ruff / mypy / pytest config, dev dependency group
uv.lock                # committed, reproducible dev installs
hacs.json              # HACS metadata; minimum HA 2025.1.0
VISION.md / CLAUDE.md / STACK.md
```

- **Package boundaries (enforced by discipline; add lint rules if they start to blur):**
  - `statistics.py` and any pure-logic helper compute over **decoded** consumption data, never raw payloads. Helen client **construction and session ownership** is confined to `coordinator.py`; the fetch helpers in `statistics.py` call Helen only through the client the coordinator hands them and never construct one.
  - `coordinator.py` is the only owner of the long-lived Helen client session and the only place that drives login, delivery-site selection, and imports on it. `config_flow.py` builds a short-lived client solely for config-flow boundary validation (credential check and delivery-site discovery/selection, as HA's config-flow rules require) and closes it before the entry is created.
  - `__init__.py` wires; it holds no business rules beyond target-entry resolution for the admin action.
  - `const.py` holds constants only — no logic.

---

## 1. Language & Runtime

- **Language:** Python — **user-owned**, do not change without explicit approval.
- **Runtime version is not freely chosen — it tracks the Home Assistant release this integration targets.** The target is HA Core **2025.1**, so the floor is **Python 3.12** (`requires-python = ">=3.12"`, ruff `target-version = "py312"`) — **user-owned**. Minimum HA is declared in `hacs.json` (`2025.1.0`) and the test harness is pinned to the same series. When the targeted HA series is bumped, re-verify the Python floor first; do not back-deploy below HA's minimum.
- **Strictness mode:** `mypy --strict` with zero errors. Additionally enable `warn_unreachable`, `warn_redundant_casts`, and `no_implicit_optional`. `from __future__ import annotations` in every module. Type checking is **the first reviewer** — prefer designs where a mistake is a type error rather than a runtime surprise. New warnings are not allowed.
- **Typing discipline:**
  - The config entry is typed: `type HelenConfigEntry = ConfigEntry[HelenConsumptionCoordinator]`, used in `async_setup_entry` / `async_unload_entry` / the target-entry resolver so `entry.runtime_data` is typed rather than `Any`. Per-entry state lives on `runtime_data` — never module-level globals or untyped `hass.data[DOMAIN]` dictionaries.
  - Model impossible states as impossible: frozen `@dataclass(frozen=True, slots=True)` for domain values, `enum.StrEnum` / `typing.Literal` for closed sets.
  - Prefer `TypedDict` for structured dict boundaries; prefer explicit narrowing over `cast`.
- **Dev-environment provisioning:** [`mise`](https://mise.jdx.dev/) is the single bootstrap. `mise install` provisions the pinned tools (Python 3.12, `uv`) from `.mise.toml` and runs `uv sync --frozen` via a postinstall hook, so a fresh checkout reaches a reproducible dev environment with one command. `.mise.toml` is the source of truth for tool/runtime versions.
- **Python dependency manager:** [`uv`](https://docs.astral.sh/uv/) (itself provisioned by mise) resolves and locks the dev/test dependencies from `pyproject.toml` into the committed `uv.lock`. The **integration's runtime dependency** is declared in `manifest.json → requirements` (HA's contract), never in `pyproject.toml`; `pyproject.toml` + `uv.lock` govern the **development / test** environment only.
- **Pinning surfaces (three layers, each owns one):** `.mise.toml` pins tool/runtime versions; `uv.lock` pins dev dependencies; `manifest.json → requirements` pins the runtime dependency exactly (`==`).

---

## 2. Frameworks

| Concern                | Framework / library                                                                                                   | Notes                                                                                          |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Platform               | Home Assistant Core integration APIs — config entries, config flow, admin service                                     | Follow core conventions and the Integration Quality Scale                                      |
| Concurrency            | `asyncio` — all Helen/recorder calls via `hass.async_add_executor_job`; `asyncio.Lock` guards overlapping polls       | Never block the event loop; no bare threads                                                    |
| Polling / data seam    | `HelenConsumptionCoordinator` driven by `async_track_time_interval`                                                   | Timer-driven, not `DataUpdateCoordinator` — no entities (see §11)                              |
| Statistics             | recorder external statistics — `async_add_external_statistics`, `statistics_during_period`, `async_adjust_statistics` | The product's actual "sink"; runs in the executor                                              |
| Config / options       | Config flow (`username`, `password`, `delivery_site_id`)                                                              | UI-based setup required; no YAML config. Delivery site auto-selected or picked from a dropdown |
| Boundary validation    | `voluptuous` (bundled with HA) for config-flow and service schemas (`cv.date`, `cv.string`)                           | Validate every user input and service payload before use                                       |
| Upstream API wrapper   | `oma-helen-cli` (`helenservice`) — the sole runtime dependency, pinned exactly in `manifest.json`                     | All Helen auth/endpoint logic lives upstream; push fixes there                                 |
| Admin action           | `async_register_admin_service` — user-triggered bounded backfill                                                      | `ServiceValidationError` for out-of-range input                                                |
| Logging                | stdlib `logging` — `_LOGGER = logging.getLogger(__name__)`                                                            | No `print()`; credentials/full responses never logged (see §8)                                 |
| Diagnostics            | `homeassistant.components.diagnostics` + `async_redact_data`                                                          | `diagnostics.py` redacts username/password/delivery_site_id                                    |
| Testing                | `pytest` + `pytest-homeassistant-custom-component` (`asyncio_mode = auto`)                                            | Pinned to the HA 2025.1 series                                                                 |
| Time in tests          | `freezegun` / HA's `async_fire_time_changed`                                                                          | Deterministic — no wall-clock sleeps                                                           |
| Lint + format          | `ruff` (lint **and** format; `E`, `F`, `I`, `UP`, `B`), pinned exactly                                                | HA core's choice; replaces black/isort/flake8/pylint                                           |
| Type checker           | `mypy --strict`                                                                                                       | The compiler-as-reviewer gate                                                                  |
| Manifest / repo checks | `hassfest` + HACS validation (GitHub Actions)                                                                         | Must pass before release; see §3                                                               |

---

## 3. Build & verify commands

Bootstrap with `mise install` before running anything below. `.mise.toml` (verify task) and `pyproject.toml` (ruff/mypy/pytest config) are the single source of truth for tool configuration. Never invoke `ruff`, `mypy`, or `pytest` with ad-hoc flags from commits, CI, or agent scripts — go through the declared commands so local and CI behaviour cannot drift.

| Variable      | Command                                                                                          |
| ------------- | ------------------------------------------------------------------------------------------------ |
| `$FORMAT_CMD` | `uv run ruff format custom_components tests`                                                     |
| `$LINT_CMD`   | `uv run ruff check custom_components tests && uv run mypy custom_components`                     |
| `$BUILD_CMD`  | `uv run python -m compileall -q custom_components` (syntax gate — Python has no compile step)    |
| `$TEST_CMD`   | `uv run pytest tests/`                                                                           |
| `$VERIFY_CMD` | `mise run verify` — uv sync --frozen → ruff check → ruff format --check → mypy → pytest, failing on the first error |

> The ecosystem's structural gates — `hassfest` and HACS validation — cannot run locally in a custom-integration repo (`script.hassfest` lives in the Home Assistant core repository), so they run in CI as the `home-assistant/actions/hassfest` and `hacs/action` GitHub Actions. `$VERIFY_CMD` is what any change must pass before claiming completion; a PR must additionally pass the hassfest and HACS-validate actions.

---

## 4. Performance budgets

- **Event loop:** MUST NOT be blocked. Every Helen API and recorder call runs in the executor (`hass.async_add_executor_job`). HA actively detects and warns on blocking calls inside the loop — treat such a warning as a build failure.
- **Setup latency:** `async_setup_entry` runs exactly one import immediately (so the Energy Dashboard has data at once) with `raise_on_error=True` → a transient failure surfaces as `ConfigEntryNotReady` (HA retries with backoff), bad credentials as `ConfigEntryAuthFailed` (reauth). Everything else is on the timer.
- **Poll interval:** default **3 hours** (`SCAN_INTERVAL`). Do not poll more often than hourly — Helen's data lags 1–2 days, so faster polling buys nothing and only costs the upstream. Overlapping polls are **skipped, not queued**, via the update lock; the client session is never touched by two runs at once.
- **Fetch window:** one poll fetches the 7-day rolling window (`ROLLING_WINDOW_HOURS`) plus one day of deliberate slack for Helen's 1-2 day publication lag — an 8-day fetch; do not widen the default rolling window without a documented reason. Upstream calls have a typed failure and an explicit fail-quiet (timer) vs raise (setup) decision — never a silent empty.
- **Memory:** avoid per-poll object churn; the client session is opened, used, and closed per update.

---

## 5. Persistence shape

- **Config entry data:** `username`, `password`, and a required `delivery_site_id` (auto-selected for single-site accounts, picked from a dropdown otherwise). Nothing else is persisted in the entry. Runtime state lives on `entry.runtime_data` (the coordinator), never in module globals.
- **Statistics:** one external statistic per config entry, `statistic_id = helen_energy_consumption:consumption_<delivery_site_id>` (non-`[a-z0-9_]` characters replaced with `_`), unit `kWh`, `has_sum = True`, cumulative `state`/`sum`.
- **Cumulative chain** is anchored to the last DB record in the query window; gaps are zero-filled and later repaired via `async_adjust_statistics`. The automatic poll never rewrites history outside the current rolling window.
- **Backfill action:** an explicit, user-triggered admin action may rebuild a bounded historical range (start date → now) for a chosen config entry, writing statistics outside the rolling window. It stays within the API's available history (bounded by the contract start date), waits for the poll lock, and MUST never corrupt or erase existing statistics on a transient failure (writes only after all Helen calls succeed).
- **Schema migration:** if the config-entry shape changes, bump `entry.version` and add `async_migrate_entry`; a decode/migration failure degrades gracefully (re-setup / re-auth), it does not crash.
- **Do not** write your own files, open databases, or persist to arbitrary paths.
- **Forbidden persistence:** anything declared forbidden in `VISION.md → Persistence and Privacy Posture`. Never persist raw upstream payloads or PII beyond the config-entry credentials.

---

## 6. Approved dependencies

Default answer to "should we add a library?" is **no** — prefer stdlib or HA core helpers. Home Assistant enforces this structurally: every runtime dependency MUST be listed in `manifest.json → requirements`, version-pinned exactly (`==`), and published on PyPI. `hassfest` validates the manifest; unpinned or unlisted imports fail validation.

**Runtime (`manifest.json → requirements`):**

| Dependency      | Pinning   | Why it earns its place                            |
| --------------- | --------- | ------------------------------------------------- |
| `oma-helen-cli` | `==1.8.0` | The only domain dependency — all Helen API access |

> `voluptuous`, `aiohttp`, the recorder, and HA helper APIs ship **with Home Assistant** — depend on the versions HA provides; do not add them to `requirements`.

**Development only (`pyproject.toml` `dev` group, synced by `uv`):**

| Dependency                              | Version                | Why it earns its place                                          |
| --------------------------------------- | ---------------------- | --------------------------------------------------------------- |
| `homeassistant`                         | `>=2025.1,<2025.2`     | Type stubs + test harness against the targeted core             |
| `pytest-homeassistant-custom-component` | `>=0.13.205,<0.14`     | Standard custom-component test fixtures, aligned to HA 2025.1   |
| `pytest`                                | `>=8.3,<9`             | Test runner                                                     |
| `ruff`                                  | `==0.15.20`            | Lint + format gate — exact-pinned; output can shift per release |
| `mypy`                                  | `>=2.1,<2.2`           | Strict type-checking gate                                       |
| `freezegun`                             | stable, explicit bound | Deterministic time in tests (**to be added when needed**)       |
| `oma-helen-cli`                         | `==1.8.0`              | Mirrors the manifest pin so the component imports in tests      |

`uv` is pinned in `.mise.toml`; `mise install` runs `uv sync --frozen`, so every dev environment resolves from the committed `uv.lock`. Bumping `ruff`, `uv`, or `mypy` is a deliberate manual step. Adding any other runtime dependency requires explicit approval and an entry here, in the same change.

---

## 7. Stack-specific reject-list additions

Product-specific:

- ❌ Blocking I/O in the event loop — any Helen/recorder call not routed through `hass.async_add_executor_job`, synchronous file I/O, `time.sleep`, or a CPU-bound loop called directly from a coroutine.
- ❌ Re-implementing Helen authentication or endpoint parsing — belongs in `oma-helen-cli`; push fixes upstream.
- ❌ Cost/price/spot/VAT/contract-type logic — out of scope per `VISION.md`.
- ❌ Writing statistics outside the current rolling window on the automatic poll path — **except** via the explicit, user-triggered backfill action. In every path, a transient failure must never erase or corrupt already-imported history.
- ❌ Adding entities, services, or Lovelace resources without a `VISION.md` decision-filter pass.
- ❌ Unpinned or floating dependency versions in `manifest.json`.

General Python / Home Assistant:

- ❌ `typing.Any` (explicit or implicit) without an inline `# reason: ...`; `cast()` that bypasses narrowing instead of a runtime guard or `TypedDict`.
- ❌ `# type: ignore` without a specific error code and inline reason naming the underlying constraint.
- ❌ Bare `except:` / `except Exception:` that swallows silently. Catch the upstream's specific exceptions and re-raise as the correct HA exception (see §8 for the taxonomy).
- ❌ Creating your own `aiohttp.ClientSession` — the `helenservice` client owns its session; do not add a parallel one.
- ❌ `print()` in shipped code — use `_LOGGER`.
- ❌ Logging secrets/credentials/PII at any level; logging full API responses.
- ❌ Module-level mutable global state for per-entry data — use `entry.runtime_data` (typed).
- ❌ Rolling your own polling with orphaned `asyncio.create_task` — background work is owned by the config entry (see §9).
- ❌ YAML-only configuration — the config flow is the only setup surface.
- ❌ Wildcard imports (`from x import *`) and imports not listed + pinned in `manifest.json`.
- ❌ Naive `datetime` objects anywhere in logic, statistics, or logs; `datetime.utcnow()` / `datetime.now()` without an explicit timezone (both produce naive or local-drifting values). See §10.
- ❌ Local-time storage or computation, and manual UTC-offset arithmetic — timezone conversion happens only at the Helen-parse / user-facing edges via `dt_util`.

---

## 8. Logging & privacy

- **Logger:** one module-level `_LOGGER = logging.getLogger(__name__)` per module. Levels are meaningful: `debug` for developer detail (e.g. "poll already in progress; skipping"), `warning` for recoverable API errors, `exception` for unexpected failures. Never log at `info` on the per-poll path.
- **Typed error taxonomy → HA behaviour.** Every failure mode maps to exactly one HA outcome (note: there is no `DataUpdateCoordinator`, so no `UpdateFailed` — see §11):

  | Failure                            | Raise / do                                  | HA behaviour                                                                  |
  | ---------------------------------- | ------------------------------------------- | ----------------------------------------------------------------------------- |
  | Transient Helen error at **setup** | `ConfigEntryNotReady`                       | Retry setup with backoff                                                      |
  | Bad/expired credentials            | `ConfigEntryAuthFailed`                     | Start reauth (setup path raises; timer path calls `entry.async_start_reauth`) |
  | Transient Helen error on **timer** | log at `warning`/`exception`, skip the tick | Integration stays loaded; history untouched                                   |
  | Invalid backfill input             | `ServiceValidationError`                    | Error shown to the user in the action UI                                      |
  | Backfill failed mid-flight         | `HomeAssistantError`                        | Error shown to the user; existing statistics untouched                        |

  Translate `HelenAuthenticationException` / `InvalidApiResponseException` into this taxonomy at `coordinator.py` — never let a raw `helenservice` exception escape into HA.

- **PII redaction:** credentials (`username`, `password`) and `delivery_site_id` are the sensitive fields. `diagnostics.py` routes diagnostics through `async_redact_data` with an explicit `TO_REDACT` set; default to redacting unknown-sensitive fields rather than exposing them.
- **Crash/telemetry reporting:** none. Home Assistant owns error reporting; no third-party analytics or crash reporters. The only outbound traffic is `oma-helen-cli` talking to Helen.

---

## 9. Background & lifecycle

- **Setup/teardown symmetry:** `async_setup_entry` registers both the `async_track_time_interval` unsub and `coordinator.close` via `entry.async_on_unload(...)`, so unload is leak-free — keep it this way. `async_unload_entry` returns honestly (`True`; there are no platforms to unload).
- **Allowed background work:** the timer-driven consumption import only. It is owned by the config entry and cancelled on unload. No orphaned `asyncio.create_task`.
- **Concurrency safety:** the update lock is created on the event loop and guards overlapping polls; the timer path is fail-quiet, the backfill path waits for the lock so it never interleaves with a scheduled poll.
- **Forbidden background work:** tasks that outlive the config entry, polling more aggressively than the product needs, and background activity that retains data forbidden by `VISION.md`.
- **Reload:** support reload on config change where feasible so updates apply without an HA restart.

---

## 10. Time & timezones

Time is treated exactly like any other external input: **UTC everywhere internally, converted only at the boundary.** This matters directly here because the product imports **hourly** statistics keyed by timestamp — a timezone slip double-counts or shifts a whole day of consumption.

- **Internal representation:** all datetimes in logic, statistics rows, and logs are **timezone-aware UTC**. Naive datetimes are forbidden (see §7).
- **Conversion happens only at the two edges:** decoding Helen's response → normalise to UTC immediately (Helen reports in Finnish local time; the conversion lives at the parse edge in `statistics.py` and nowhere else); any user-facing or log rendering → convert at the last moment. Nothing in between ever holds local time.
- **Plain dates are not exempt:** `date.today()` is host-timezone-dependent — the same hazard class as naive datetimes, and banned in logic. Calendar dates at the Helen edge (the poll window end, backfill bounds) are computed as the **Europe/Helsinki** local date, the calendar Helen's API expects; a UTC date would lag Helsinki by 2-3 hours daily. Everything internal stays UTC.
- **Statistics rows:** every `StatisticData` `start` MUST be an hour-aligned, timezone-aware UTC datetime; backfill range math is done in UTC. HA stores and computes in UTC and renders in the user's configured timezone — do not fight this.
- **Python mechanics:** use `datetime.now(UTC)` and aware datetimes. Never `datetime.utcnow()` or `datetime.now()` (both banned — naive/local). Never hand-roll `timedelta` offset math for timezones.
- **Home Assistant mechanics:** use `homeassistant.util.dt` (`dt_util`) rather than raw `datetime` for anything time-of-day-aware — `dt_util.utcnow()` for "now", `dt_util.parse_datetime()` / `dt_util.as_utc()` to normalise inbound values at the boundary, `dt_util.as_local()` only when producing a user-facing value.
- **Tests:** time-sensitive tests use `freezegun` / `async_fire_time_changed` — deterministic, no wall-clock dependence.

---

## 11. Intentional Divergences

Deliberate departures from Home Assistant's standard integration patterns, with the reason on record:

| Date       | Standard pattern                                         | Divergence                                                                      | Reason                                                                                                                                                                                          |
| ---------- | -------------------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-04 | `DataUpdateCoordinator[T]` as the polling/data seam      | Thin custom `HelenConsumptionCoordinator` driven by `async_track_time_interval` | There are no entities; `DataUpdateCoordinator`'s value is fanning typed data out to `CoordinatorEntity` subscribers. Revisit if entities are ever added.                                        |
| 2026-07-04 | Async upstream client using the shared `aiohttp` session | Synchronous `helenservice` client, every call routed through the executor       | `oma-helen-cli` is the only maintained Helen library and it is sync. The client owns its own session (opened/closed per update). Prefer pushing an async client upstream over wrapping locally. |
| 2026-07-04 | Module-level mutable state for per-entry data is banned (§7) — per-entry state lives on `entry.runtime_data` | `statistics.py` keeps a module-level `_CHAIN_LOCKS` registry, one `asyncio.Lock` per `statistic_id` | Serializes chain writes across a config-entry reload, when the old and new coordinator instances briefly coexist and write the same chain (#18); `runtime_data` cannot span instances across a reload. This is per-`statistic_id` cross-reload coordination, not per-entry data. Revisit a typed `HassKey` in `hass.data` if the cosmetic warts (never-pruned dict, test reset in `tests/conftest.py`) ever grow beyond cosmetic. |
| 2026-07-04 | Every Helen client call runs via `hass.async_add_executor_job` (§0, §4) | `coordinator.close` — the sync `entry.async_on_unload` teardown callback — calls the client's `close()` on the event loop; the setup-failure paths in `__init__.py` reuse it | `close()` is near-instant local session teardown with no network round-trip, and executor-routing it would rework the teardown wiring §9 says to keep. All poll, backfill, and re-login closes are executor-routed (#31 / PR #39). Revisit if the upstream client's `close()` ever grows real I/O. |
