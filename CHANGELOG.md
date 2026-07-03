# Changelog — energipays-client

All notable changes to this project are documented here.
Format: `[YYYY-MM-DD] — description`.

---

## [2026-07-03] — v0.2.0 — Observable, non-circular AES key resolution

### Fixed
- **Fast-path key extraction failures are now visible** — the exception (or the
  candidate-count mismatch) is logged at WARNING with the error type instead of
  being swallowed at DEBUG level. This hid the real failure behind the generic
  "AES key not set" for six bridge releases.
- **Circular-dependency trap removed** — when the fast path fails and no valid
  Bearer token exists, `_ensure_key()` now raises an actionable error naming the
  underlying cause and the manual overrides, instead of recursing into
  `login()` and dying with "AES key not set".
- **Fast path now collects candidates across all chunks** and requires exactly
  one unique `Base64.parse` match (was: first chunk with exactly one match).
- `_fetch_encrypted_payload()` has a 20s timeout (previously could hang forever).

### Changed
- `pyproject.toml` version 0.1.0 → 0.2.0.

---

## [2026-06-26] — Human-friendly `devices`, `device`, `status` + boost sync with bridge CLI

### Added
- `devices` command now renders a human-friendly device list by default (name, online/offline
  badge, ID, server, target temp, boost power); `--json` gives raw response
- `device` command now renders a human-friendly device detail panel (name, ID, online badge,
  target temp, boost power, volume, firmware, serial, LoRa, switcher states); `--json` for raw
- `boost --power {25,50,75,100}` — optionally set boost power and trigger the boost in a single
  command (mirrors `boost start --power` in energipays-bridge)
- `boost` now checks if a boost is already active before sending; exits with current temps if so
  (mirrors bridge CLI fail-fast behaviour)

### Changed
- **`boost` default period changed from 1 (30min) to 2 (1h)** — aligned with energipays-bridge
  CLI default; was inconsistent between the two tools
- `_format_status`, `_format_devices`, `_format_device` share a common `_c()` color helper
- `_POWER_MAP`, `_POWER_LABEL`, `_PERIOD_LABEL` constants extracted for reuse across commands

---

## [2026-06-26] — Human-friendly `status` display + global `--json` flag

### Added
- `python cli.py status` now renders a human-friendly terminal dashboard by default:
  - Device name, live timestamp, separator
  - Hot Water: T3/T2/T1 temps with ASCII fill bars (█░) vs target °C, heater state
  - Boost: ON/OFF badge + current power % setting
  - Power Flow: grid kW with import ↓/export ↑ direction, solar ☀, heater kW
  - Switchers: PowerDiverter, Off-Peak, Solar Restriction, Priority Mode, Weather Boost
    (● on / ○ off)
  - Active Rule: resolved rule name (fetched via `client.rules()`, best-effort)
  - Signal: WiFi dBm + LoRa on/off
- ANSI colors (bold headers, green=live/export, yellow=import, red=boost ON/heater ON,
  gray=off-states) with automatic fallback to plain text when:
  - stdout is not a tty (piped/redirected) — output suitable for `grep`, `tee`, etc.
  - `NO_COLOR` env var is set (https://no-color.org)
- Global `--json` flag: `python cli.py --json status` (or any command) outputs raw JSON
  instead of human-friendly text — useful for scripting and shell pipelines

### Changed
- `_first_device()` now returns `(device_id, data_server, name)` — name is available
  for the status header without a second API call
- `main()` dispatches on return type: `str` → `print()`, anything else → `_out()` (JSON)

---

## [2026-06-26] — CLI completed: cancel + power subcommands

### Added
- `cli.py cancel` — cancel a running boost (`cancel_boost`, sends `period=1` to data server)
- `cli.py power --percent 25|50|75|100` — set boost power level (`set_boost_power`)
- Updated CLI docstring with full example set for all write commands

### Changed
- HANDOVER.md: confirmed boost `period` semantics (1=30min, 2=1h, 3=2h), cancel
  mechanism, boost power endpoint, and all switcher write endpoints
- HANDOVER.md: replaced unresolved "TODO" on period semantics with confirmed table
- HANDOVER.md: extended write endpoint table with weather/solar/priority switchers
  and `boostPower` endpoint

---

## [2026-06-24] — Initial working release

### Added (energipays.py)
- `EnergipaysClient` — authenticated Energipays API client
  - Email/password credential auth (env vars `ENERGIPAYS_EMAIL` / `ENERGIPAYS_PASSWORD` or constructor args)
  - JWT access token cache (`.token_cache.json`), proactive refresh, re-login on 401
  - AES-256-CBC encryption/decryption (CryptoJS wire format)
  - Dynamic AES key extraction from deployed JS bundle (`_ensure_key`)
  - `ENERGIPAYS_KEY` env var → `.key_cache.json` → live extraction (validated by round-trip decrypt)
- Read methods: `me()`, `devices()`, `device()`, `device_status()`, `rules()`,
  `statistics()`, `stats()`, `logs()`, `storages()`, `messages()`, `subscription()`,
  `ping()`, `client_device()`, `client_devices_priority()`, `client_defaults()`
- Write methods: `set_device_status()`, `update_device()`, `set_boost_power()`,
  `boost_device()`, `cancel_boost()`, `cancel_weather_boost()`,
  `create_rule()`, `update_rule()`, `delete_rule()`

### Added (cli.py)
- Full argparse CLI with subcommands:
  - `me`, `devices`, `device`, `status`, `rules`, `logs` — read-only
  - `statistics`, `stats` — telemetry with date range and phase options
  - `boost --period 1|2|3 --yes` — trigger timed boost
  - `device-on`, `device-off` — PowerDiverter enable/disable
  - `heater-on`, `heater-off` — immersion heater control
  - `key` — resolve and print the AES key
- Credential resolution: `--email`/`--password` → `ENERGIPAYS_EMAIL`/`ENERGIPAYS_PASSWORD` → interactive prompt
- `--password-stdin` for secure scripting (avoids shell history exposure)

### Added (extract_key.py)
- Dynamic AES key discovery by fetching and grepping the live JS bundle

### Removed
- All Chrome/browser CDP code (`cdp_tokens.py`, `token_server.py`)
- `pychrome` dependency

### Fixed
- Login endpoint: `POST /api/auth/login` with CSRF bootstrap and `username` field
  (not `email`) — confirmed against HAR of real browser session
- `_post()`: encrypted bodies sent as form-urlencoded `payload=`, not JSON
- Stale Bearer header on login request causing spurious 401 before credentials checked
