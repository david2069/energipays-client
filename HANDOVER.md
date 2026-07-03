# HANDOVER — energipays-client

Context for whoever (Claude Code or a human) picks this up next. Written 2026-06-24.

## Goal

Standalone Python client + CLI for the Energipays API (https://energipays.com),
reverse-engineered from the React SPA. No browser dependency. Encrypted requests,
credential login, device telemetry + control (boost).

## Current status — where it actually stands

The library was originally built around pulling a JWT out of a logged-in Chrome
tab (CDP). That was **ripped out** by request; auth is now pure email/password.
Along the way the login implementation was wrong three times; a HAR capture of a
real browser session (`login.har`, in repo) gave ground truth and the login path
was corrected to match the SPA byte-for-byte.

**LOGIN IS VERIFIED WORKING (2026-06-24).** A live run authenticated and returned
a real `GET /api/client/device/{id}` payload — full chain confirmed: CSRF bootstrap
→ `username`/password login → Passport Bearer → encrypted call decrypted. Read
endpoints are good. Remaining work is boost semantics + refresh (below).

### Files

| File | Purpose | State |
|------|---------|-------|
| `energipays.py` | Client library (`EnergipaysClient`) | Login rewritten to match HAR; needs live verification |
| `cli.py` | argparse CLI, subcommands, credential flags | Works (offline-tested); blocked only on login |
| `extract_key.py` | Dynamic AES key extractor from JS bundle | Working; verified |
| `diag_login.py` | Login-response inspector (prints shape + JWT claims) | Working diagnostic |
| `find_login.py` | Greps the JS bundle for the auth flow | Working diagnostic |
| `login.har` | **Ground-truth** HAR of a full real session | Reference data (gitignore it — contains live tokens) |
| `CLAUDE.md` | Project conventions | Up to date (no Chrome refs) |
| `.token_cache.json` / `.key_cache.json` | Cached secrets | gitignored; may hold stale tokens — safe to delete |

Deleted: `cdp_tokens.py`, `token_server.py` (Chrome/browser token capture).
`pychrome` removed from `requirements.txt` (still physically installed in `venv/`).

## Authentication — confirmed ground truth (from `login.har`)

This is Laravel **Sanctum (session/CSRF)** for login + **Passport** Bearer tokens
for the API. Flow:

1. `GET /sanctum/csrf-cookie` (fallback `GET /`) → sets `XSRF-TOKEN` +
   `prod_main_session` cookies.
2. Set header `X-XSRF-TOKEN` = URL-decoded `XSRF-TOKEN` cookie value.
3. `POST /api/auth/login`
   - `Content-Type: application/x-www-form-urlencoded`
   - body: `payload=<urlencode(encrypted JSON)>`
   - **plaintext payload fields: `{"username": <email>, "password": <pw>}`**
     (field is `username`, NOT `email` — confirmed by decrypting the HAR).
   - No Authorization header (a stale Bearer here causes a 401 "Unauthenticated"
     *before* credentials are checked — this bit us repeatedly).
4. Response (encrypted, `{"payload": …}`) decrypts to:
   `{token_type: "Bearer", expires_in: 300, access_token, refresh_token,
     web_version, mobile_version}`.
5. Subsequent API calls use `Authorization: Bearer <access_token>` only
   (cookies/XSRF not required once you hold the Bearer token — verified earlier).

Access token lifetime is **300s**. `refresh_token` is an opaque (non-JWT) string.

### Open auth question
- **`/api/auth/refresh` is unverified.** The HAR session was short so no refresh
  fired. `refresh()` currently posts `{"refresh_token": …}` to `/api/auth/refresh`
  — endpoint/shape is a guess. Because credentials are available, `_ensure_token()`
  falls back to a full re-login when refresh fails, so this isn't blocking, but it
  should be confirmed against a real refresh (let a token expire, or capture one).

## Encryption — confirmed

- AES-256-CBC, CryptoJS wire format: `base64(JSON({value, iv, mac}))`.
  `value`=base64(ciphertext), `iv`=base64(16B), `mac`=HMAC-SHA256(key, iv_b64+value_b64).
- Key is in the JS bundle (`main.*.chunk.js`), rotates per deploy. Resolved at
  runtime: `ENERGIPAYS_KEY` env → `.key_cache.json` → dynamic extraction
  (validated by round-trip decrypt). Current key: `w0oBD4tK…LqwA=` (in cache).
- **All encrypted POSTs are form-urlencoded `payload=…`** (confirmed for
  `/api/auth/login` and `/api/users/settings`), NOT JSON. `_post()` was fixed to
  match. Encrypted responses come back as JSON `{"payload": …}`.
- Data-server endpoints (`data-au-1.energipays.com`) are **plaintext**,
  form-urlencoded, Bearer-authed.

## Write endpoints — confirmed (HAR 2026-06-24, extended 2026-06-26)

All POST/PUT to main API (energipays.com), AES-256-CBC encrypted form `payload=`.
Data-server endpoints are plaintext form-urlencoded.

| Action | Method | Path | Body | Server |
|--------|--------|------|------|--------|
| PowerDiverter on/off | POST | `/api/devices/{id}/status` | `{"status": 1\|0}` | main |
| Heater on/off | POST | `/api/devices/{id}/status` | `{"heaterStatus": 1\|0}` | main |
| Off-peak switcher | POST | `/api/devices/{id}/status` | `{"offpeakSwitcherStatus": 1\|0}` | main |
| Weather switcher | POST | `/api/devices/{id}/status` | `{"weatherSwitcherStatus": 1\|0}` | main |
| Solar restriction | POST | `/api/devices/{id}/status` | `{"solarRestrictionSwitcherStatus": 1\|0}` | main |
| Priority mode | POST | `/api/devices/{id}/status` | `{"priorityModeSwitcherStatus": 1\|0}` | main |
| Boost power | POST | `/api/devices/{id}/boostPower` | `{"boost_power": 1\|2\|3\|4}` | main |
| Boost start | POST | `/api/client/device/{id}/boost` | `period=1\|2\|3` (form) | data |
| Boost cancel | POST | `/api/client/device/{id}/boost` | `period=1` (form) | data |
| Full state sync | PUT | `/api/devices/{id}` | `{"status_data": {...}, "rule_id": "<uuid>\|\"0\"", "type": "command"}` | main |

**Full `status_data` fields** (PUT body, from HAR):
```json
{
  "admin": 1, "customer": 1, "retailer": 1, "evStatus": 0,
  "heaterStatus": 1, "heaterPumpStatus": 0, "heater2SwitcherStatus": 1,
  "offpeakSwitcherStatus": 0, "weatherSwitcherStatus": 0,
  "priorityModeSwitcherStatus": 1, "smartWeatherSwitcherStatus": 0,
  "solarRestrictionSwitcherStatus": 1, "offpeakTemperatureSwitcherStatus": 1
}
```
The full PUT is sent when changing the active rule (`rule_id` = UUID or `"0"` for none).

Rule CRUD (create/edit/delete) endpoints are **unconfirmed** — capture a HAR while editing a rule.

---

## Endpoint map (from HAR, all 200 unless noted)

Main API (`energipays.com`, encrypted, Bearer):
- `POST /api/auth/login` — login (see above)
- `GET  /api/users/me` — returns name, email, phone, address, role ("Customer")
- `POST /api/users/settings` — update settings (encrypted form payload). Known fields from Profile page:
  - `language` — e.g. `"en"`
  - `temperature_format` — `"celsius"` or `"fahrenheit"`
  - Push notification toggles (bool): `device_status`, `off_peak_status`, `device_online_status`,
    `device_temperature_threshold_status`, `boost_status`, `done_status`, `new_chat_message`
  - `device_temperature_threshold` — numeric °C (e.g. `40`)
  - **Push notifications are non-functional** — toggles exist in the UI but no notifications have
    ever been received. Likely Energipays has not configured/paid for an APNs or FCM push service.
    Don't rely on these for any integration.
  - Endpoint/field names are guesses from UI labels — capture a HAR while saving settings to confirm exact keys.
- `GET  /api/devices`
- `GET  /api/rules`
- `GET  /api/groups`
- `GET  /api/avatars`
- `GET  /api/geo/countries`, `/api/geo/{id}/states`, `/api/geo/{id}/cities`
- **No logout call** — logout is client-side only (clears localStorage). Nothing to implement.

Data server (`data-au-1.energipays.com`, plaintext form, Bearer):
- `POST /api/client/device/status` — body `ids[0]=<id>`
- `POST /api/client/device/{id}/statistics`
- `POST /api/client/device/{id}/stats`
- `POST /api/client/device/{id}/boost` — **body `period=<int>`** (see below)
- `GET  /api/client/device/{id}` — single device live state

Other hosts (not yet wrapped, probably out of scope):
- `message-1.energipays.com` — `/api/ping`, `/api/contacts`
- `price-data-1.energipays.com` — `/PD_PriceAPI/get_last_price_and_demand`

## Boost — CONFIRMED (2026-06-26)

Period is an enum index, **not minutes**:

| period | duration |
|--------|----------|
| 1 | 30 min |
| 2 | 1 hour |
| 3 | 2 hours |

**Cancel**: there is no separate cancel endpoint. `cancel_boost()` sends
`POST /boost` with `period=1` — confirmed from the energipays.com SPA source.

**Boost power**: separate endpoint `POST /api/devices/{id}/boostPower` with
`{"boost_power": idx}` where idx 1=25%, 2=50%, 3=75%, 4=100%.
Client method: `set_boost_power(device_id, power_idx)`.

## Device object + scheduling (from live `GET /api/client/device/{id}`)

The Sunamp device (`d2784559-…`) returns a large object. Highlights useful for
building features:
- Live telemetry under `telemetryData` (water temps, voltages, power, SoC, signal,
  `boostStatus`, `currentDRM`, etc.) and a flattened `status_data`.
- `boost_power` (4), `current_temperature` (85), `offpeak_temperature`,
  `antibacterial_temperature`, `weather_boost_power`/`weather_boost_time` (3),
  `is_online`, `boostStatus` (0 = not boosting), `weather_boost_ends_at`.
- **Weekly schedule** in `device_statuses.customer_rule.d1..d7`. Each day is a list
  of time slots: `{timeFrom, timeTo, command, boost_power, temperature_min?}`.
  **This confirms the `CMD_IDLE="1"` / `CMD_BOOST="2"` constants** already in
  `energipays.py`: `command:"1"` = idle (boost_power 0), `command:"2"` = boost
  (with `boost_power` + `temperature_min`). A future "set schedule" feature would
  PUT/POST this structure back (endpoint not yet captured — likely under `/api/rules`
  or a device-server route; capture a HAR while editing the schedule to confirm).

## Fixes already applied (this session)
- Removed hardcoded AES key; added dynamic extraction + cache.
- Deleted all Chrome/browser code; `pychrome` dropped from requirements.
- Rewrote auth to credential login (`ENERGIPAYS_EMAIL`/`ENERGIPAYS_PASSWORD` or
  constructor args), token cache, JWT-expiry-aware proactive refresh, re-login on 401.
- Fixed login: correct endpoint `/api/auth/login`, CSRF bootstrap, `username` field,
  form-urlencoded `payload=`, no stale Authorization header.
- Fixed `_post()` to send encrypted bodies as form-urlencoded `payload=`.

## Known data gaps

- `solarStreamA/B/C` reports 0/N/A when solar output is zero — cause unknown (hardware, firmware, LoRa, CT sensor, or cloud API). Don't treat `solarStreamA == 0` as a confirmed zero reading.
- `offPeakStreamHeaterPower` always 0/N/A — no off-peak tariff configured on this account.
- `stateOfCharge` is always 0 — Thermino SOC appears unimplemented in the API despite showing in the UI (confirmed 0% with temps T1=56/T2=44/T3=39°C, which clearly isn't empty).
- Phase fields (`voltageB/C`, `phasePowerB/C`) are 0 on single-phase installs — 0 means "phase absent", not zero watts. Multi-phase installs would have non-zero B/C values. `phasePower` is always the sum across active phases and is the right field to use for single-value display.

## Modal → API field mapping (all from `statistics()` + `device_status()`, no extra calls needed)

| Modal | UI label | API field |
|-------|----------|-----------|
| Grid | Current import kW (live, total) | `status.phasePower` (sum of all active phases) |
| Grid | Per-phase voltage (V) | `status.voltageA/B/C` (0 = phase not present) |
| Grid | Per-phase power (kW) | `status.phasePowerA/B/C` (0 = phase not present) |
| Grid | Export today/yesterday/7d/14d kWh | `{period}.EEct` |
| Grid | Import today/yesterday/7d/14d kWh | `{period}.IEct` |
| PowerDiverter | Diverted heating kWh | `{period}.DE_h` |
| PowerDiverter | Diverted plug kWh | `{period}.DE_e` |
| PowerDiverter | Off-peak heating kWh | `{period}.OIE_h` |
| PowerDiverter | Import max/min/avg | `{period}.IEct_max/min/avg` |
| Thermino | Temperature displayed (avg) | average of T1+T2+T3 — computed client-side, not a separate field |
| Thermino | Temperature T1 bottom (live) | `status.waterTemperature1` — hottest (heating element at base) |
| Thermino | Temperature T2 middle (live) | `status.waterTemperature2` |
| Thermino | Temperature T3 top (live) | `status.waterTemperature3` — coolest |
| Thermino | Heating on/off | `status.heaterStatus` |
| Thermino | SOC % | `status.stateOfCharge` — **always 0, appears unimplemented** |
| Thermino | T° setting | `status.temperatureLimit` |
| Thermino | PD rule name | `device_statuses.customer_rule` name field (e.g. "Daily boost & disable") |
| Thermino | Off-Peak rule name | separate rule object — "Empty" when none configured |

## Remaining open questions

1. **`/api/auth/refresh`** — still unverified. Falls back to re-login, so not blocking.
2. **Rule CRUD endpoints** — create/edit/delete are now used by `energipays-bridge` via
   the same `EnergipaysClient.create_rule/update_rule/delete_rule` methods; working in
   production but the exact request shapes haven't been captured in a HAR.
3. **Cancel weather boost** — `cancel_weather_boost()` wraps
   `POST /api/client/device/{id}/weather/boost/cancel`; not yet tested live.

## Gotchas learned the hard way
- A stale Bearer header on an unauthenticated request → 401 "Unauthenticated"
  regardless of credentials. Always strip Authorization for login/refresh.
- The client preloads cached tokens, so `client._token` being truthy does NOT mean
  login succeeded. Check the **login response** for a token, not `client._token`.
  (`cli.py` and the `energipays.py` probe both do this correctly now.)
- Sandbox/dev note: this was developed in an environment with no network to
  energipays.com and no `pycryptodome` — use `cryptography` (installed) to
  decrypt HAR payloads; live calls must be run on the user's machine.
- Login payload field is `username` even though it's an email address.
