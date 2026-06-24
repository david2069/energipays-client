#!/usr/bin/env python3
"""
Reverse-engineer the real login flow from the deployed JS bundle.

Prints code context around auth-related tokens (login URL, OAuth grant params,
client_id / client_secret) so we can see exactly how the SPA authenticates.

Run:  python find_login.py
"""
from __future__ import annotations

import re

import requests

from extract_key import discover_chunk_urls

BASE = "https://energipays.com"

# Patterns that reveal the auth flow.
NEEDLES = [
    "oauth/token", "grant_type", "client_secret", "client_id",
    "users/login", "auth/login", "/login", "refresh_token", "password_grant",
]


def main() -> None:
    s = requests.Session()
    urls = discover_chunk_urls(BASE, s)
    print(f"scanning {len(urls)} chunk(s)…\n")

    seen: set[str] = set()
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        try:
            text = s.get(url, timeout=30).text
        except Exception as e:
            print(f"  ({name}: fetch failed: {e})")
            continue
        for needle in NEEDLES:
            for m in re.finditer(re.escape(needle), text):
                i = m.start()
                ctx = text[max(0, i - 140): i + 140]
                key = needle + ctx[:40]
                if key in seen:
                    continue
                seen.add(key)
                print(f"[{name}] …{ctx}…\n")


if __name__ == "__main__":
    main()
