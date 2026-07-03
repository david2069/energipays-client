# energipays-client

Standalone Python client library and CLI for the [Energipays](https://energipays.com)
API. Handles AES-256-CBC encrypted requests, credential-based JWT authentication, and
device control — no browser required.

---

## Requirements

- Python 3.11+
- `requests` ≥ 2.31
- `pycryptodome` ≥ 3.19

---

## Installation

### Option A — copy the single file (simplest)

`energipays.py` is self-contained. Copy it into your project:

```bash
cp energipays.py /your/project/energipays.py
pip install requests pycryptodome
```

### Option B — editable install from this repo

```bash
git clone <this-repo> energipays-client
cd energipays-client
pip install -e .          # once pyproject.toml is added, or:
pip install requests pycryptodome
```

### Option C — install into another project's venv (e.g. energipays-bridge)

```bash
cd /your/other/project
source venv/bin/activate
pip install /path/to/energipays-client/energipays.py   # direct file
# or, for live edits to reflect immediately:
pip install -e /path/to/energipays-client
```

---

## Authentication

The API uses JWT Bearer tokens (5-minute lifetime). The client logs in with
email + password, caches tokens, auto-refreshes near expiry, and re-logs-in
on a 401 — all transparently.

**Set credentials via environment variables (recommended):**

```bash
export ENERGIPAYS_EMAIL="you@example.com"
export ENERGIPAYS_PASSWORD="your-password"
```

Tokens are cached in `.token_cache.json` so subsequent runs refresh instead of
re-sending the password.

**AES key** (required for encrypted API calls) is resolved automatically:
`ENERGIPAYS_KEY` env var → `.key_cache.json` → live extraction from the JS bundle.
You never need to set this manually — the client extracts and caches it on first use.

---

## CLI usage

```bash
# Read commands (no --yes required)
python cli.py me                                    # your account profile
python cli.py devices                               # list all devices
python cli.py status                                # live status — human-friendly dashboard
python cli.py --json status                         # raw JSON dump (global --json flag)
python cli.py rules                                 # list automation rules
python cli.py logs --limit 10                       # recent activity log
python cli.py statistics                            # aggregate totals (today/week/etc)
python cli.py stats --from 2026-06-01 --to 2026-06-26   # per-day chart data
python cli.py key                                   # show the resolved AES key

# Write commands (all require --yes as a safety guard)
python cli.py boost --period 2 --yes               # start boost: 1=30min 2=1h 3=2h
python cli.py cancel --yes                         # cancel running boost
python cli.py power --percent 75 --yes             # set boost power: 25/50/75/100%
python cli.py device-on --yes                      # enable PowerDiverter
python cli.py device-off --yes                     # disable PowerDiverter
python cli.py heater-on --yes                      # turn immersion heater on
python cli.py heater-off --yes                     # turn immersion heater off
```

### Credential flags

```bash
# Env vars (preferred — don't appear in shell history)
ENERGIPAYS_EMAIL=you@example.com ENERGIPAYS_PASSWORD=secret python cli.py devices

# Explicit flags (avoid --password; it appears in ps and shell history)
python cli.py -e you@example.com --password-stdin me   # password read from stdin

# Explicit (less secure)
python cli.py -e you@example.com -p secret me
```

### Targeting a specific device

All commands default to the first device on your account. Pass a device ID to
target another:

```bash
python cli.py status d2784559-3e77-4af9-a872-ca03b0b2728d
python cli.py boost d2784559-... --period 1 --yes
```

---

## Library usage

### Basic

```python
from energipays import EnergipaysClient

# Uses ENERGIPAYS_EMAIL / ENERGIPAYS_PASSWORD env vars
client = EnergipaysClient()

# Or pass credentials directly
client = EnergipaysClient(email="you@example.com", password="secret")

# Read
print(client.me())
print(client.devices())
print(client.rules())

# Live device status (returns telemetry + status_data)
status = client.device_status(["d2784559-3e77-4af9-a872-ca03b0b2728d"])

# Historical data
stats = client.statistics("d2784559-...", "https://data-au-1.energipays.com")
```

### Device control

```python
device_id = "d2784559-3e77-4af9-a872-ca03b0b2728d"
data_server = "https://data-au-1.energipays.com"

# Boost
client.boost_device(device_id, data_server, period=2)   # 1=30min, 2=1h, 3=2h
client.cancel_boost(device_id, data_server)

# Boost power (1=25%, 2=50%, 3=75%, 4=100%)
client.set_boost_power(device_id, 4)

# Switch fields (all via POST /api/devices/{id}/status)
client.set_device_status(device_id, status=1)                        # PD on
client.set_device_status(device_id, status=0)                        # PD off
client.set_device_status(device_id, heaterStatus=1)                  # heater on
client.set_device_status(device_id, offpeakSwitcherStatus=1)         # off-peak on
client.set_device_status(device_id, weatherSwitcherStatus=0)         # weather off
client.set_device_status(device_id, solarRestrictionSwitcherStatus=1)
client.set_device_status(device_id, priorityModeSwitcherStatus=0)

# Change active rule (full state sync required)
status_data = {
    "admin": 1, "customer": 1, "retailer": 1, "evStatus": 0,
    "heaterStatus": 1, "offpeakSwitcherStatus": 0,
    # ... other fields from a prior device_status() call
}
client.update_device(device_id, status_data, rule_id="<uuid>")  # or "0" for no rule
```

### Rule management

```python
# List
rules = client.rules()

# Create (returns new rule with id)
new_rule = client.create_rule("My Rule", rule_type="command")

# Update schedule
client.update_rule(rule_id, {
    "name": "Morning Boost",
    "data": {
        "d1": [{"timeFrom": "06:00", "timeTo": "08:00", "command": 2,
                "boost_power": 4, "temperature_min": 55}],
        # d2..d7 same structure; omit a day key to have no schedule that day
    }
})

# Delete
client.delete_rule(rule_id)
```

---

## Using in your own web app (e.g. energipays-bridge pattern)

The client is synchronous (`requests`-based). In an async application (FastAPI,
asyncio) wrap calls with `asyncio.to_thread`:

```python
import asyncio
from energipays import EnergipaysClient

client = EnergipaysClient()   # credential + key resolution happen lazily

# In an async FastAPI route or background task:
result = await asyncio.to_thread(client.device_status, [device_id])

# Polling loop (60s)
async def poll_loop():
    while True:
        status = await asyncio.to_thread(client.device_status, [device_id])
        stats  = await asyncio.to_thread(client.statistics, device_id, data_server)
        # ... process and publish
        await asyncio.sleep(60)
```

### Sharing one client instance

Create the client once at app startup and share it — the client is thread-safe
for concurrent reads (it holds a `requests.Session` internally and uses a lock
for token refresh):

```python
# main.py / lifespan
from energipays import EnergipaysClient

client = EnergipaysClient()
client.login()   # eager login; raises on bad credentials

app.state.client = client
app.state.device_id = client.devices()[0]["id"]
app.state.data_server = client.devices()[0]["server"]
```

### Getting the data server URL

The data server URL is per-device, returned by `client.devices()`:

```python
devices = client.devices()
dev = devices[0]           # or find by id
device_id  = dev["id"]
data_server = dev["server"]   # e.g. "https://data-au-1.energipays.com"
```

Pass `data_server` to `boost_device()`, `cancel_boost()`, `statistics()`, `stats()`.

---

## Key API response shapes

### `device_status([device_id])`

Returns live telemetry. Key fields:

| Field | Description |
|-------|-------------|
| `waterTemperature1/2/3` | Bottom/mid/top tank temp (°C) |
| `temperatureLimit` | Target temp setting (°C) |
| `boostStatus` | 1 = boosting, 0 = idle |
| `boost_power` | Current power index (1–4 = 25–100%) |
| `phasePower` | Total grid power (kW; negative = import) |
| `solarStreamA` | Solar input (kW) |
| `offPeakStreamHeaterPower` | Heater draw (kW) |
| `heaterStatus` | 1 = heater on |
| `sd.customer` | 1 = PowerDiverter enabled |
| `signalWiFi` | WiFi signal (dBm) |
| `active_rule_id` | UUID of active rule, `""` or `"unknown"` = none |

### `statistics(device_id, data_server)`

Returns aggregate totals. Key prefixes: `today.*`, `yesterday.*`, `week.*`.

| Field | Description |
|-------|-------------|
| `{period}.IEct` | Grid import kWh |
| `{period}.EEct` | Grid export kWh |
| `{period}.DE_h` | Diverted solar → heat kWh |
| `{period}.DE_e` | Diverted solar → plug kWh |

---

## Security notes

- Never commit `.token_cache.json` or `.key_cache.json` — both contain live secrets
- Never hardcode the AES key — it rotates on each Energipays deploy; use dynamic extraction
- Prefer `ENERGIPAYS_EMAIL`/`ENERGIPAYS_PASSWORD` env vars over `--password` CLI flag
  (the flag appears in `ps` output and shell history)
- Access tokens expire after 300 seconds; the client handles refresh automatically

---

## Files

| File | Purpose |
|------|---------|
| `energipays.py` | `EnergipaysClient` library — the only file you need to import |
| `cli.py` | Command-line interface (argparse, all subcommands) |
| `extract_key.py` | Dynamic AES key extractor from the live JS bundle |
| `diag_login.py` | Login response inspector (debugging) |
| `CHANGELOG.md` | Version history |
| `HANDOVER.md` | Deep technical context: auth flow, encryption, confirmed endpoints |
