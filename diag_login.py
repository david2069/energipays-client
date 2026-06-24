#!/usr/bin/env python3
"""
Diagnostic: inspect exactly what POST /api/users/login returns, so we can see
why its token is rejected by subsequent API calls. Prints the response shape and
decodes any JWT claims (no signatures, token values truncated).

Run:  python diag_login.py
"""
from __future__ import annotations

import base64
import json

from energipays import EnergipaysClient


def jwt_claims(tok: str) -> dict:
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception as e:
        return {"_decode_error": str(e)}


def short(v):
    if isinstance(v, str) and len(v) > 48:
        return v[:24] + "…(%d chars)" % len(v)
    return v


def main() -> None:
    c = EnergipaysClient(auto_login=False)
    resp = c.login()

    print("== /api/users/login response ==")
    print("python type:", type(resp).__name__)
    if isinstance(resp, dict):
        print("top-level keys:", list(resp.keys()))
        for k, v in resp.items():
            if isinstance(v, dict):
                print(f"  {k}: <dict keys={list(v.keys())}>")
            elif isinstance(v, list):
                print(f"  {k}: <list len={len(v)}>")
            else:
                print(f"  {k}: {short(v)!r}")

        # Decode any JWT-looking strings (top-level and one level deep).
        def walk(prefix, d):
            for k, v in d.items():
                if isinstance(v, str) and v.startswith("eyJ") and v.count(".") == 2:
                    print(f"\n== JWT claims for '{prefix}{k}' ==")
                    print(json.dumps(jwt_claims(v), indent=2, default=str))
                elif isinstance(v, dict):
                    walk(f"{prefix}{k}.", v)

        walk("", resp)
    else:
        print(json.dumps(resp, indent=2, default=str)[:1500])

    # Show which token we ended up using, and probe one endpoint with it.
    print("\n== token actually stored as Bearer ==")
    print("token:", short(c._token or ""))
    if c._token:
        print("claims:", json.dumps(jwt_claims(c._token), indent=2, default=str))

    print("\n== probe GET /api/users/me with stored token ==")
    r = c.session.get(c.base_url + "/api/users/me")
    print("status:", r.status_code, "| body:", r.text[:200])


if __name__ == "__main__":
    main()
