# VISION

## Vision

A dead-simple Home Assistant integration that gets your Helen electricity
**consumption** into the Energy Dashboard — and does nothing else. It should be
the thing you recommend to someone who says "I just want my Helen kWh in Home
Assistant, I don't care about prices."

## Goal

Import hourly electricity consumption from Oma Helen into Home Assistant's
long-term statistics as a single cumulative `kWh` stream that the Energy
Dashboard consumes directly. Correct, low-maintenance, and boring.

## Core Principles

1. **Consumption only.** One number: kWh. Every feature request is measured
   against "does this help get consumption into the Energy Dashboard?"
2. **Thin wrapper, not a reimplementation.** All Helen API work lives in the
   upstream `oma-helen-cli` library. We orchestrate; we do not re-implement
   authentication or endpoint parsing.
3. **Correct cumulative chain over completeness.** Helen's data lags 1–2 days.
   A statistics chain that stays consistent (zero-fill then repair) matters more
   than showing the freshest possible hour.
4. **No entities unless they earn their place.** The integration runs in the
   background. We add a sensor only if it materially helps the user trust the
   data.
5. **Fail quiet, keep history.** A transient API failure must never corrupt or
   erase already-imported statistics.

## Product Shape

- A HACS-installable custom integration (`custom_components/helen_energy_consumption`).
- Config flow: Oma Helen credentials, optional delivery-site pick when the
  account has several meters.
- A background poll (default every 3 h) that extends the statistics chain.
- A technical backfill action (Home Assistant Developer Tools → Actions) that
  imports or repairs a bounded historical range for a chosen delivery site.
- A read-only diagnostics download (Home Assistant's standard integration
  diagnostics) with credentials and delivery-site id redacted, for locally
  debugging a stalled import.
- Output: one external statistic per config entry, `kWh`, `has_sum`, ready for
  the Energy Dashboard.

## Non-Goals

- **No cost, price, or tariff tracking** — no spot prices, no fixed/market
  contract logic, no VAT, no transfer fees. (That is the full
  `oma-helen-ha-integration`'s job.)
- **No dashboards, cards, or Lovelace resources** shipped with the integration.
- **No production/return-to-grid or gas** unless Helen exposes it and users ask.
- **No end-user backfill UI.** A _technical_ backfill action (Developer Tools →
  Actions) is provided to import history on onboarding and to repair corrupted
  ranges; there is no consumer-facing backfill UI, and the automatic rolling
  window remains the normal mechanism.
- **No config for the poll interval** unless a real need appears.

## Decision Filter

For any proposed change, ask in order:

1. Does it help get **consumption** into the Energy Dashboard, or keep that
   correct? If no → **reject**.
2. Can it be done without re-implementing Helen API logic that
   `oma-helen-cli` already owns? If no → **reject or push upstream**.
3. Does it keep the integration installable and understandable in one sitting
   (the six-file shape)? If it adds significant surface → **narrow it**.
4. Could it corrupt or erase existing statistics on failure? If yes → **reject
   until proven safe**.

## Success Definition

- A new user installs via HACS, signs in, and within one poll cycle (allowing
  for Helen's lag) sees their consumption in the Energy Dashboard.
- Once running, it needs zero attention: no manual re-imports, no drift, no
  double-counting across restarts.
- The whole component stays readable in a single review.

## Persistence and Privacy Posture

- **Credentials** (Oma Helen username/password) are stored only in the Home
  Assistant config entry, handled by HA core. They are never logged.
- **Consumption data** is written only to the local HA recorder/statistics
  database. Nothing is sent anywhere except to Helen's own API during fetch.
- No telemetry, analytics, or third-party calls. The only outbound network
  traffic is `oma-helen-cli` talking to Helen.
