# Energipays cloud API — functional contract

The shared contract for every Energipays client: this library (and energipays-bridge
through it), energipays-mobile, and energipays.com itself. Only **behaviour** is shared:
endpoints, bodies, value ranges, signs and labels. UI stays per client.

Unofficial: the API is private to energipays.com (Power Diverter Pty Ltd) and may change
at any time. Every entry names its evidence and is marked **verified** (a live read) or
**site** (read from the energipays.com bundle, not yet sent live by this library) or
**unverified**. Ids are fakes (`00000000-0000-4000-8000-000000000001`). Request/response
examples are in `tests/fixtures/`.

## Hosts and transport

| Host | Body | Auth | Status |
|---|---|---|---|
| `https://energipays.com` | AES-256-CBC (CryptoJS) `payload=<base64>` form, encrypted JSON responses | Bearer | verified 2026-06-24 |
| device `server` (e.g. `https://data-au-1.energipays.com`) | plain form, plain JSON | Bearer | verified 2026-06-24 |
| `https://message-1.energipays.com` | `/api/ping`, `/api/contacts` | — | site; not wrapped |

- Login: `POST /api/auth/login` with `{username, password}` after the Sanctum CSRF cookie
  (verified 2026-06-24). Tokens live ~300 s.
- Site refreshes the token every 140 s, up to 3 tries 3 s apart (site).
- On a 401 the site's data-server helper waits 1 s and retries once (site). This library
  refreshes and retries once on both hosts.
- **HTTP 202 = queued** for the device, not applied (site `queueHandler`). The library
  returns `{"queued": true, "status": 202, "body": …}`.

## Rule activation

`PUT /api/devices/{id}` with
`{"status_data": <current status_data, circuit field set>, "rule_id": "<uuid>|0", "type": "<circuit>"}`.

| Circuit `type` | `status_data` field |
|---|---|
| `command` (Power Diverter) | `rule_customer` |
| `offpeak` | `rule_offpeak` |
| `heater2` | `rule_heater2` |

- Read the device first (`GET /api/devices/{id}`; the object may be wrapped in `data`).
  Change **only** the circuit field; never `admin` / `customer` / `retailer`.
- The site sends it only when `group_id` is `"0"`, `"empty"`, null or missing; a grouped
  device's rules belong to the group (site).
- `"0"` clears the circuit's rule.
- The short body `{rule_id, type}` returned **HTTP 500** live; the full body applied
  (energipays-mobile, 2026-11-03, PD circuit, customer account). **verified**
- Rule type spelling is `offpeak`, never `off_peak` (site).
- `POST /api/devices/{id}/clearRules {"rule": "all"}` unbinds all three circuits; the site
  sends it before enabling Weather boost (site).

## Switches

`POST /api/devices/{id}/status` with one field, `1|0`: `status` (Power Diverter),
`heaterStatus`, `offpeakSwitcherStatus`, … (HAR 2026-06-24). The site writes the PD switch
as `{status}` and reads it back from `status_data.customer` (site).

## Boost (data server)

`POST /api/client/device/{id}/boost` form `period=<n>`:

| period | meaning | evidence |
|---|---|---|
| 1 | cancel (site sends 1 to cancel; 2026-06-26 notes called it 30 min) | site + HAR 2026-06-26 |
| 2 | 1 hour | site |
| 3 | 2 hours | site |
| 4 | 3 hours | site |

Boost power: `POST /api/devices/{id}/boostPower {"boost_power": 1|2|3|4}` = 25/50/75/100 %
(HAR 2026-06-26).

## Statistics (data server)

- `POST …/statistics` form `tz, phase` → buckets `today`, `yesterday`, `week`,
  `fortnight`. "Last 14 days" is **`fortnight`** (site).
- `POST …/stats` form `dateFrom, dateTo, tz, data_type, phase`. Energy view sends
  `data_type=consumption`, Power view `data_type=power` (site). `phase` is `sum|a|b|c`
  (site). `tz` is the device's timezone, not a fixed one.
- Grid sign: `phasePower > 0` is export; in the power series `EIP < 0` is import (site).

### Series labels

Power view: `EIP_I` Home import, `EIP_E` Home export, `SP` Solar gen, `WHD` Diverted
heater, `EPD` Diverted extra, `GLPH` Heater, `GLPE` Extra plug, `OLPH` Off-Peak heater,
`OLPE` Off-Peak extra, `Av_temp` Average T (site; energipays-mobile checked live
2026-11-01).

Energy view: `EEct` export, `IEct` import, `Einv` solar, `DE_h` / `DE_e` diverted
heater / plug, `IE_h` / `IE_e` grid heater / plug, `OIE_h` / `OIE_e` off-peak heater / plug
(site).
