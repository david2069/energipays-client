# CLAUDE.md — energipays-client

## Project

Python client library for the Energipays API (https://energipays.com), reverse-engineered from the React SPA bundle. Handles AES-256-CBC encrypted requests/responses and token management via Chrome DevTools Protocol.

## Setup

```bash
cd ~/dev/Claude/Projects/energipays-client
source venv/bin/activate
pip install -r requirements.txt
```

## Authentication

The API uses short-lived JWT Bearer tokens (5 min expiry). Authentication is handled automatically via Chrome DevTools Protocol — no manual token copying required.

**Prerequisite:** Chrome must be running with `--remote-debugging-port=9222` (already configured) and `https://energipays.com` must be open in a tab while logged in.

```python
from energipays import EnergipaysClient
client = EnergipaysClient.from_chrome()   # pulls fresh token from Chrome automatically
```

## Key Files

- `energipays.py` — main client library (`EnergipaysClient` class)
- `cdp_tokens.py` — Chrome DevTools Protocol token extractor
- `token_server.py` — fallback local HTTP server for token capture (Safari alternative)

## Encryption

- AES-256-CBC via CryptoJS wire format: `base64(JSON({value, iv, mac}))`
- Key hardcoded in JS bundle (`main.*.chunk.js`), loaded via `ENERGIPAYS_KEY` env var
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
- `requests` for HTTP, `pycryptodome` for AES, `pychrome` for CDP
- Never commit `.token_cache.json` — contains live auth tokens
- Never commit the AES key directly — use `ENERGIPAYS_KEY` env var

## Commands

```bash
python cdp_tokens.py          # verify Chrome token extraction works
python energipays.py          # run full API probe (requires Chrome open + logged in)
```
