## What changed

<!-- One or two sentences. Refs #N (never Fixes/Closes). -->

## Severity and nature

<!-- For a defect fix: sev:S1-blocker | S2-high | S3-medium | S4-low. Label the PR to match.
     State the regression risk (low / medium / high) and what it touches. -->

## Evidence

<!-- Per `CLAUDE.md`, "Ground every claim in evidence". Cite the source for every claim about
     a cloud field or endpoint: the energipays.com bundle (file + function), a live read
     (time, account role, device type, ids masked), the verified parts of energipays-client,
     or the code. Say what is unverified. -->

## Mutation check

<!-- Required for code changes. "A test that passes with the fix disabled proves nothing."
     Disable the change, run the tests, and paste which ones failed: -->

Disabled the change; these tests failed:

## Verification

<!-- Tick what applies, and say what was NOT run.
  - [ ] Full test suite passes (offline; no live cloud, dummy AES key)
  - [ ] Write paths checked with the request logged and not sent (READ_ONLY / mocked transport)
  - [ ] Live account, read-only
  - [ ] Live write the owner approved in this session, with read-back and undo noted
-->

## Changelog

- [ ] `CHANGELOG.md` updated under `[Unreleased]`, or this change is not user-facing
      (internal refactor, tests, docs or policy only)

## Publishable evidence

- [ ] `python3 scripts/check_pii.py --scan` passes — no device or account ids, emails,
      LAN IPs, home paths, tokens or AES key fragments; no HAR files or screenshots added
