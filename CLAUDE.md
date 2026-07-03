# CLAUDE.md — energipays-client

## Project

Standalone Python client library for the Energipays API (https://energipays.com),
reverse-engineered from the React SPA bundle. Handles AES-256-CBC encrypted
requests/responses and credential-based JWT authentication. No browser required.

## Setup

```bash
cd ~/dev/Claude/Projects/energipays-client
source venv/bin/activate
pip install -r requirements.txt
```

## Authentication

The API uses short-lived JWT Bearer tokens (~5 min expiry). The client logs in with
email/password (`POST /api/users/login`), caches the returned access + refresh
tokens, auto-refreshes when the JWT nears expiry, and re-logs-in on a 401 — all
automatically.

Provide credentials via env vars (preferred) or constructor args:

```bash
export ENERGIPAYS_EMAIL="you@example.com"
export ENERGIPAYS_PASSWORD="..."
```

```python
from energipays import EnergipaysClient

client = EnergipaysClient()                       # uses ENERGIPAYS_EMAIL/PASSWORD
# or
client = EnergipaysClient(email="...", password="...")

print(client.me())
print(client.devices())
```

Tokens are cached in `.token_cache.json`, so subsequent runs refresh instead of
re-sending the password. Pass `auto_login=False` to defer the network call.

## Key Files

- `energipays.py` — main client library (`EnergipaysClient` class)
- `cli.py` — command-line client (argparse, subcommands, credential flags)
- `extract_key.py` — dynamic AES key extractor from the deployed JS bundle

## Encryption

- AES-256-CBC via CryptoJS wire format: `base64(JSON({value, iv, mac}))`
- The key lives in the client-side JS bundle (`main.*.chunk.js`) and rotates on each
  deploy, so it is resolved at runtime — never hardcoded. Resolution order:
  `ENERGIPAYS_KEY` env var → `.key_cache.json` → dynamic extraction
  (`extract_key.find_key`), validated by round-trip decrypting a live response.
- `EnergipaysClient` resolves the key lazily via `_ensure_key()`, so any call works
  once authenticated. To inspect candidates manually: `python extract_key.py`.
- Data server endpoints (`data-au-1.energipays.com`) are **not** encrypted

## API Structure

| Server | Purpose |
|--------|---------|
| `https://energipays.com` | Main API — encrypted JSON, Bearer token |
| `https://data-au-1.energipays.com` | Device telemetry — form-encoded, unencrypted |
| `https://message-1.energipays.com` | Ping/messaging |

## Known Device

- Device ID: `d2784559-3e77-4af9-a872-ca03b0b2728d` (Sunamp hot water diverter)
- Data server: `https://data-au-1.energipays.com`
- Timezone: `Australia/Sydney`

## Conventions

- Python >= 3.11, type hints on public functions
- `requests` for HTTP, `pycryptodome` for AES
- Never commit `.token_cache.json` or `.key_cache.json` — contain live secrets
- Never hardcode the AES key — use `ENERGIPAYS_KEY` env var or dynamic extraction

## Commands

```bash
python extract_key.py    # discover the AES key candidate(s) in the live JS bundle
python energipays.py     # standalone API probe (uses ENERGIPAYS_EMAIL/PASSWORD or prompts)

# CLI client (credential flags fall back to env vars, then prompt):
python cli.py --help
python cli.py -e you@example.com -p secret me
python cli.py devices                     # uses ENERGIPAYS_EMAIL/PASSWORD
python cli.py -e you@example.com --password-stdin me   # password via stdin (safer)
python cli.py stats <device_id> --from 2026-06-01 --to 2026-06-24
python cli.py boost <device_id> --period 2 --yes   # 1=30min, 2=1h, 3=2h
python cli.py cancel --yes                          # cancel running boost
python cli.py power --percent 75 --yes             # set boost power to 75%
python cli.py device-on --yes                       # enable PowerDiverter
python cli.py device-off --yes                      # disable PowerDiverter
python cli.py heater-on --yes / heater-off --yes   # immersion heater control
```
