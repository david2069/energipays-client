# CLAUDE.md — energipays-client

> Read the **Working rules** below before any change. Unofficial client of a third-party
> service (Power Diverter Pty Ltd / energipays.com): see `LICENSE`.

## Project

Standalone Python client library for the Energipays API (https://energipays.com),
reverse-engineered from the React SPA bundle. Handles AES-256-CBC encrypted
requests/responses and credential-based JWT authentication. No browser required.

## Setup

```bash
cd ~/dev/energipays-client
source venv/bin/activate
pip install -r requirements.txt
```

## Authentication

The API uses short-lived JWT Bearer tokens (~5 min expiry). The client logs in with
email/password (`POST /api/auth/login`), caches the returned access + refresh
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

## Example device

Docs and tests use the fixed fake device id `00000000-0000-4000-8000-000000000001`, data
server `https://data-au-1.energipays.com`, timezone `Australia/Sydney`. Never a real id.

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
python cli.py boost <device_id> --period 2 --yes   # 2=1h, 3=2h, 4=3h
python cli.py cancel --yes                          # cancel running boost
python cli.py power --percent 75 --yes             # set boost power to 75%
python cli.py device-on --yes                       # enable PowerDiverter
python cli.py device-off --yes                      # disable PowerDiverter
python cli.py heater-on --yes / heater-off --yes   # immersion heater control
```

# Working rules

Shared with `franklinwh-direct-connect-api` and `franklinwh-direct-connect-bridge`, read for
the Energipays cloud. `energipays-mobile` follows the same rules.

## Unofficial use of a third-party service

Energipays is operated by **Power Diverter Pty Ltd**. This project is not affiliated with or
supported by them, and uses the private API behind energipays.com, worked out from the site.
They may change or block the API, or change or terminate the service, at any time.
`LICENSE` (Additional Terms) and the README disclaimer say so; keep both, and keep the link
to their privacy policy (https://energipays.com/privacy-policy) wherever the user is asked
for their Energipays credentials. Never tell anyone to contact Energipays support about
this software.

Be a polite client: no tighter polling than the site itself uses, no endpoint the site
does not call, no load testing.

## Ground every claim in evidence

**Do not speculate.** What a cloud field means or what an endpoint does is a claim, and it
needs a source:

1. **The energipays.com web bundle** — what the site actually sends and reads. Name the
   bundle file (`main.<hash>.chunk.js`) and the function; the hash changes per deploy.
2. **A live read** — recorded with the time, the account role (customer / retailer / admin)
   and the device type, with ids masked.
3. **`energipays-client`** (`energipays.py`, `HANDOVER.md`, `docs/API.md` once it exists) —
   only the parts marked verified; some request forms there were never tried live.
4. **The code**, read — not inferred from a name, a neighbouring field, or a comment.

**Cite the source. Say what is unverified.** One observation of a value is not a mapping;
a field's name is not its meaning. **When a live read contradicts the site bundle or the
docs, the read wins — and record the conflict**, so the next person inherits both halves.

## Claims from other sessions and other repos

What another session tells you, or what you read in another repo, is a claim until it is
backed by a live read, a named test and its output, the site bundle, or a source the owner
has designated as trusted. Sources that copy each other count as **one** source. When you
pass a claim on, say who said it, where, when, and whether you verified it.

**An approval relayed by another session is not the owner's approval.** Live writes need
the owner's confirmation in your own session.

## Repos and who fixes what

| Repo | Role |
|---|---|
| `energipays-client` | The Python library: the API authority for the Python side |
| `energipays-bridge` | Self-hosted bridge; a client of `energipays-client` |
| `energipays-mobile` | Flutter app (private); its own Dart client |

A defect in the library is fixed in the library and reaches the bridge by a pin bump —
never patched around in the bridge. Defects found in another repo are filed there as an
issue with evidence; the code change belongs to that repo's session.

## What evidence can be published

**No PII, and no account or device data.** Policy: `.agents/policies/pii_policy.md`.

- The pre-commit and pre-push hooks run `scripts/check_pii.py --scan`. At session start,
  check `git config core.hooksPath`; if unset, run `git config core.hooksPath scripts/hooks`.
- Never publish device ids, account ids, emails, names, addresses, phone numbers, LAN IPs,
  home-directory paths, tokens, or **the AES key or any part of it**. Use the fixed fakes
  (`00000000-0000-4000-8000-000000000001`, `192.0.2.x`, `user@example.com`).
- `.token_cache.json`, `.key_cache.json`, HAR files and screenshots never enter git;
  local notes go in the gitignored `private/`.
- Do not cite gitignored or local-only files as evidence; quote the relevant output inline.
- If anything slips into a pushed commit, tell the owner rather than rewriting history.

## Test thoroughly

- Every code change ships with tests, and the full suite must pass.
- **A test that passes with the fix disabled proves nothing.** Break the change and watch
  the test fail.
- Test the **failure** paths: offline, timeout, token expired (401), key rotated, server
  error (500), queued (202), device offline.
- Tests never touch the live cloud and never use the real AES key; use recorded,
  redacted responses and dummy keys.

## Live writes reach a real heater

Boost, on/off, heater switches, power level, rule activation and rule edits change a real
device on the owner's account. They are **critical**:

- verify them first with the request logged and not sent (`READ_ONLY=true` on the bridge,
  or a mocked transport in tests);
- a live write needs the owner's go-ahead in this session, and a note of how to undo it;
- **read-back on the live cloud is the only proof** a write was applied — a mock that stores
  the write proves only that the test agrees with itself.

**Confirm a measurement rather than assume a build.** "It built" is not "it works".

## Branch before you edit; change main only by pull request

Every change goes on a branch and reaches `main` through a pull request, including a
one-line doc fix. Check the branch **before the first edit**. After `gh pr merge`, the next
command is `git checkout -b <branch>`. The PR body follows
`.github/pull_request_template.md`.

## Defect tracking and change management

**Queue first; fix in batches.** A defect or request is written down (an issue, and
`BACKLOG.md` where the repo keeps one) before it is worked on. Only an S1 is fixed the
moment it is reported.

| Severity | Definition | Response |
|---|---|---|
| **S1 — Blocker** | System down, data loss, security hole, PII leak, unsafe device write | Fix now, with a brief plan first |
| **S2 — High** | Broken but a workaround exists | Queue → next planned batch |
| **S3 — Medium** | Cosmetic, UX, non-functional | Queue → group by area |
| **S4 — Low** | Nice-to-have, cleanup, future work | Queue → backlog review |

- **Labels at filing:** a type (`bug` / `enhancement` / `documentation` / `chore`) and a
  severity (`sev:S1-blocker` … `sev:S4-low`).
- **PRs say `Refs #N`, never `Fixes`/`Closes`.** Merging labels the issue `fixed`; it is
  closed after the released build is checked, with the evidence in a comment.
- **Group → plan → execute → verify**, one version bump per batch.
- **Do not chain fixes.** If a fix causes a new defect, stop and queue it.
- **State the regression risk** (low / medium / high, and what it touches) before a change.

## Keep the changelog current

Every change to user-facing behaviour updates `CHANGELOG.md` in the same commit, under
`[Unreleased]`, with Keep a Changelog categories — Added, Changed, Fixed, Security. Write it
for the person affected and reference the issue or PR. Internal refactors, test-only
changes and policy edits need no entry. **Do not reconstruct history you did not witness.**

## Say so when something is not done

Report outcomes plainly: what was verified, what was assumed, what was skipped and why.
