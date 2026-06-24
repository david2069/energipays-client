"""
Dynamically extract the Energipays AES-256-CBC key from the deployed JS bundle.

Why: the key lives in client-side JavaScript (so it is inherently public — anyone
who loads the site can read it), but the bundle filename is content-hashed
(``main.<hash>.chunk.js``) and changes on every deploy. Hardcoding either the key
or the filename is brittle. This module discovers the chunk URLs at runtime and
scans them for the key.

Discovery:  GET /asset-manifest.json  -> list of hashed .js chunks
            (fallback: parse <script src> + static/js/*.js refs out of index.html)
Candidates: every 44-char base64 string that decodes to exactly 32 bytes.
Validation: a caller-supplied ``validator(key_bytes) -> bool`` picks the real key.
            The robust validator (see EnergipaysClient._ensure_key) round-trip
            decrypts a real API response: the correct key is the only one that
            yields valid PKCS#7 padding + parseable JSON.

No login required for discovery — only validation needs a live token.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import urllib.parse
from typing import Callable

import requests

KEY_CACHE = pathlib.Path(__file__).parent / ".key_cache.json"

# 32 raw bytes -> 44 base64 chars (43 + one '=' pad). Matches CryptoJS-emitted keys.
_B64_KEY_RE = re.compile(r"[A-Za-z0-9+/]{43}=")
# High-signal: the key is read via CryptoJS  enc.Base64.parse("<key>").
# The bundle also contains inline PNGs and config strings that coincidentally
# decode to 32 bytes, so context-matched candidates are tried first.
_PARSE_CTX_RE = re.compile(r"""Base64\.parse\(\s*["']([A-Za-z0-9+/]{43}=)["']""")
# JS references like  src="/static/js/main.abcd1234.chunk.js"  or bare paths in code.
_JS_REF_RE = re.compile(r"""['"(]?(/?static/js/[\w.\-]+\.js)\b""")


def _is_32_bytes(b64: str) -> bool:
    try:
        return len(base64.b64decode(b64)) == 32
    except Exception:
        return False


def _abs(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def discover_chunk_urls(base_url: str, session: requests.Session | None = None) -> list[str]:
    """Return absolute URLs of every JS chunk referenced by the deployed app."""
    s = session or requests.Session()
    urls: list[str] = []

    # Preferred: CRA asset-manifest.json lists every emitted file.
    try:
        r = s.get(_abs(base_url, "asset-manifest.json"), timeout=10)
        if r.ok:
            manifest = r.json()
            files = manifest.get("files", {})
            for v in files.values():
                if isinstance(v, str) and v.endswith(".js"):
                    urls.append(_abs(base_url, v))
            for v in manifest.get("entrypoints", []):
                if isinstance(v, str) and v.endswith(".js"):
                    urls.append(_abs(base_url, v))
    except Exception:
        pass

    # Fallback: scrape index.html for script references.
    if not urls:
        try:
            r = s.get(base_url, timeout=10)
            if r.ok:
                for m in _JS_REF_RE.finditer(r.text):
                    urls.append(_abs(base_url, m.group(1)))
        except Exception:
            pass

    # Dedupe, preserve order. Put main.* first — the key historically lives there.
    seen: set[str] = set()
    deduped = [u for u in urls if not (u in seen or seen.add(u))]
    deduped.sort(key=lambda u: (0 if "/main." in u else 1, u))
    return deduped


def candidates_from_js(js_text: str) -> list[str]:
    """Return 32-byte base64 candidates in ``js_text``, highest-signal first.

    Candidates inside an ``enc.Base64.parse("…")`` call come first (that is how
    CryptoJS loads the key); the looser whole-bundle scan follows as a fallback.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(cand: str) -> None:
        if cand not in seen and _is_32_bytes(cand):
            seen.add(cand)
            out.append(cand)

    for m in _PARSE_CTX_RE.finditer(js_text):   # high-signal context first
        add(m.group(1))
    for m in _B64_KEY_RE.finditer(js_text):     # fallback: anything 32 bytes
        add(m.group(0))
    return out


def context_candidates_from_js(js_text: str) -> list[str]:
    """Return only 32-byte candidates found inside a Base64.parse("…") call."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PARSE_CTX_RE.finditer(js_text):
        c = m.group(1)
        if c not in seen and _is_32_bytes(c):
            seen.add(c)
            out.append(c)
    return out


def gather_candidates(base_url: str, session: requests.Session | None = None) -> list[str]:
    """Download every JS chunk and collect all 32-byte base64 key candidates."""
    s = session or requests.Session()
    candidates: list[str] = []
    seen: set[str] = set()
    for url in discover_chunk_urls(base_url, s):
        try:
            r = s.get(url, timeout=20)
            if not r.ok:
                continue
        except Exception:
            continue
        for cand in candidates_from_js(r.text):
            if cand not in seen:
                seen.add(cand)
                candidates.append(cand)
    return candidates


def _load_cache() -> str:
    if KEY_CACHE.exists():
        try:
            return json.loads(KEY_CACHE.read_text()).get("key", "")
        except Exception:
            pass
    return ""


def _save_cache(key_b64: str) -> None:
    try:
        KEY_CACHE.write_text(json.dumps({"key": key_b64}))
    except Exception:
        pass


def find_key(
    base_url: str,
    validator: Callable[[bytes], bool] | None = None,
    session: requests.Session | None = None,
    use_cache: bool = True,
    save: bool = True,
) -> str:
    """Resolve the AES key (base64).

    Resolution order: cache (if it still validates) -> scan bundle for candidates.
    If ``validator`` is given, returns the first candidate it accepts; otherwise
    returns the sole/first candidate found (and raises if the choice is ambiguous).
    """
    if use_cache:
        cached = _load_cache()
        if cached and (validator is None or validator(base64.b64decode(cached))):
            return cached

    candidates = gather_candidates(base_url, session)
    if not candidates:
        raise RuntimeError(
            "No 32-byte base64 key candidates found in the JS bundle — "
            "the build layout may have changed."
        )

    if validator is not None:
        for cand in candidates:
            if validator(base64.b64decode(cand)):
                if save:
                    _save_cache(cand)
                return cand
        raise RuntimeError(
            f"Found {len(candidates)} key candidate(s) but none decrypted a sample "
            "response. Is the access token valid / the sample actually encrypted?"
        )

    if len(candidates) > 1:
        raise RuntimeError(
            f"Ambiguous: {len(candidates)} 32-byte candidates found and no validator "
            "supplied. Pass a round-trip validator to disambiguate."
        )
    if save:
        _save_cache(candidates[0])
    return candidates[0]


if __name__ == "__main__":
    import sys

    base = sys.argv[1] if len(sys.argv) > 1 else "https://energipays.com"
    s = requests.Session()

    ctx: list[str] = []
    for url in discover_chunk_urls(base, s):
        try:
            r = s.get(url, timeout=20)
            if r.ok:
                ctx.extend(c for c in context_candidates_from_js(r.text) if c not in ctx)
        except Exception:
            continue

    if ctx:
        print(f"Likely AES key(s) — found inside Base64.parse(...):")
        for c in ctx:
            print(f"  {c}")
        if len(ctx) == 1:
            print("\nSingle high-signal candidate — almost certainly the AES key.")
        else:
            print("\nMultiple — confirm with a round-trip validator (the client does this).")
    else:
        cands = gather_candidates(base, s)
        print(f"No Base64.parse() match. Fallback scan found {len(cands)} 32-byte "
              f"candidate(s) (noisy — includes inline images). Use a validator.")
        for c in cands[:10]:
            print(f"  {c}")
