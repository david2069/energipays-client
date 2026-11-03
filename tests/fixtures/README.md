# Fixtures

Request/response shapes for the Energipays cloud, with fake ids only
(`00000000-0000-4000-8000-000000000001`). They are the shared examples that
`docs/API.md` describes; energipays-mobile can check its client against the same files.

- `device.json` — `GET /api/devices/{id}` after decryption: the fields rule activation
  reads (`group_id`, `status_data`). Values are illustrative, not a capture. `customer: 0`
  is deliberate: activating a rule must not turn it to 1.
