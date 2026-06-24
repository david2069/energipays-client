"""
Energipays API client — standalone, credential-based.

Authentication: email/password login (POST /api/users/login) which returns a
short-lived JWT access token plus a refresh token. The client logs in lazily,
auto-refreshes when the JWT nears expiry, and re-logs-in on a 401. Provide
credentials via ENERGIPAYS_EMAIL / ENERGIPAYS_PASSWORD or constructor args.

Encryption: AES-256-CBC via CryptoJS.
Wire format (both directions): base64( JSON({ value, iv, mac }) )
  value = base64(AES-CBC ciphertext)
  iv    = base64(random 16-byte IV)
  mac   = HMAC-SHA256(key, iv_b64 + value_b64).hexdigest()

The AES key is resolved at runtime (env var -> cache -> dynamic extraction from
the deployed JS bundle); it is never hardcoded. See _ensure_key / extract_key.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import urllib.parse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

BASE_URL = "https://energipays.com"

# Schedule command constants (customer_rule.d1..d7 time-slot command field)
CMD_IDLE  = "1"   # standby / idle
CMD_BOOST = "2"   # boost (uses boost_power / temperature_min)

# AES-256-CBC key. Resolution order (see EnergipaysClient._ensure_key):
#   1. ENERGIPAYS_KEY env var
#   2. .key_cache.json (written by a previous dynamic extraction)
#   3. dynamic extraction from the deployed JS bundle (extract_key.find_key),
#      validated by round-trip decrypting a live response.
# The key is NOT hardcoded — it lives in client-side JS and rotates on each deploy.
_KEY_CACHE = pathlib.Path(__file__).parent / ".key_cache.json"


def _load_key_cache() -> str:
    if _KEY_CACHE.exists():
        try:
            return json.loads(_KEY_CACHE.read_text()).get("key", "")
        except Exception:
            pass
    return ""


_KEY_B64 = os.environ.get("ENERGIPAYS_KEY", "") or _load_key_cache()
_KEY = base64.b64decode(_KEY_B64) if _KEY_B64 else b""


def set_key(b64_key: str) -> None:
    """Set the AES key at runtime (alternative to ENERGIPAYS_KEY env var)."""
    global _KEY
    _KEY = base64.b64decode(b64_key)


# ── Credentials & token cache ───────────────────────────────────────────────────
# The client authenticates by email/password (no browser). Credentials come from
# env vars or constructor args. Tokens obtained from login/refresh are cached so a
# subsequent run can refresh instead of re-sending the password.
_EMAIL = os.environ.get("ENERGIPAYS_EMAIL", "")
_PASSWORD = os.environ.get("ENERGIPAYS_PASSWORD", "")

_TOKEN_CACHE = pathlib.Path(__file__).parent / ".token_cache.json"


def _load_token_cache() -> tuple[str, str]:
    """Load (access_token, refresh_token) from .token_cache.json if it exists."""
    if _TOKEN_CACHE.exists():
        try:
            d = json.loads(_TOKEN_CACHE.read_text())
            return d.get("access_token", ""), d.get("refresh_token", "")
        except Exception:
            pass
    return "", ""


# ── Crypto helpers ────────────────────────────────────────────────────────────

def decrypt(payload: str) -> dict:
    """Decrypt a base64-encoded encrypted API response."""
    if not _KEY:
        raise RuntimeError("AES key not set — call set_key() or set ENERGIPAYS_KEY")
    outer = json.loads(base64.b64decode(payload))
    ciphertext = base64.b64decode(outer["value"])
    iv = base64.b64decode(outer["iv"])
    cipher = AES.new(_KEY, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return json.loads(plaintext.decode("utf-8"))


def encrypt(data: str) -> str:
    """Encrypt a string payload for sending to the API (mirrors JS k() function)."""
    if not _KEY:
        raise RuntimeError("AES key not set — call set_key() or set ENERGIPAYS_KEY")
    iv = secrets.token_bytes(16)
    cipher = AES.new(_KEY, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(data.encode("utf-8"), AES.block_size))
    value_b64 = base64.b64encode(ciphertext).decode()
    iv_b64 = base64.b64encode(iv).decode()
    mac_msg = (iv_b64 + value_b64).encode("utf-8")
    mac = hmac.new(_KEY, mac_msg, hashlib.sha256).hexdigest()
    return base64.b64encode(
        json.dumps({"value": value_b64, "iv": iv_b64, "mac": mac}).encode()
    ).decode()


def _jwt_exp(token: str) -> int | None:
    """Return the JWT ``exp`` (unix seconds) without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


# ── Client ────────────────────────────────────────────────────────────────────

class EnergipaysClient:
    """Authenticated Energipays API client."""

    def __init__(self, email: str | None = None, password: str | None = None,
                 base_url: str = BASE_URL,
                 data_server: str = "https://data-au-1.energipays.com",
                 auto_login: bool = True):
        self.base_url = base_url.rstrip("/")
        self._data_server = data_server.rstrip("/")
        self._email = email if email is not None else _EMAIL
        self._password = password if password is not None else _PASSWORD
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": base_url,
            "Referer": base_url + "/my-devices",
        })
        self._resolving_key = False  # re-entry guard for _ensure_key()
        self._token: str | None = None
        self._refresh_token: str | None = None

        # Reuse cached tokens if present, so we don't re-send the password each run.
        cache_access, cache_refresh = _load_token_cache()
        if cache_refresh:
            self._refresh_token = cache_refresh
        if cache_access:
            self._set_access_token(cache_access)

        if auto_login:
            # Lazy auth: only hit the network if we don't already have a live token.
            if self._token_expired():
                self._ensure_token()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _set_access_token(self, token: str) -> None:
        self._token = token
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _store_tokens(self, access: str, refresh: str | None) -> None:
        """Set the access token and persist (access, refresh) to the cache."""
        self._set_access_token(access)
        if refresh:
            self._refresh_token = refresh
        try:
            _TOKEN_CACHE.write_text(json.dumps({
                "access_token": access,
                "refresh_token": self._refresh_token or "",
            }))
        except Exception:
            pass

    def _ensure_csrf(self) -> None:
        """Obtain the Laravel XSRF-TOKEN + session cookie and set X-XSRF-TOKEN.

        Login uses Sanctum session/CSRF protection: the server rejects the POST as
        "Unauthenticated" unless the request carries the session cookie and a
        matching X-XSRF-TOKEN header. We fetch these programmatically (no browser).
        """
        for path in ("/sanctum/csrf-cookie", "/"):
            try:
                self.session.get(self.base_url + path, timeout=15)
            except Exception:
                continue
            xsrf = self.session.cookies.get("XSRF-TOKEN")
            if xsrf:
                # Browser sends the URL-decoded cookie value as the header.
                self.session.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)
                return

    def login(self, email: str | None = None, password: str | None = None) -> dict:
        """POST /api/auth/login — Sanctum/CSRF credential login; stores + caches tokens.

        Wire format mirrors the SPA: encrypted payload sent as a form field
        (``payload=<base64>``, Content-Type x-www-form-urlencoded), with the CSRF
        header/cookies established first.
        """
        email = email if email is not None else self._email
        password = password if password is not None else self._password
        if not email or not password:
            raise RuntimeError(
                "No credentials — set ENERGIPAYS_EMAIL / ENERGIPAYS_PASSWORD "
                "or pass email=/password= to EnergipaysClient()."
            )
        self._ensure_key()
        self._ensure_csrf()
        payload = encrypt(json.dumps({"username": email, "password": password}))
        r = self.session.post(
            self.base_url + "/api/auth/login",
            data=urllib.parse.urlencode({"payload": payload}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = self._handle(r)
        token = resp.get("access_token") or resp.get("token") or resp.get("accessToken")
        if token:
            self._store_tokens(token, resp.get("refresh_token"))
        return resp

    def refresh(self) -> dict:
        """POST /api/auth/refresh — exchange refresh_token for a new access_token."""
        if not self._refresh_token:
            return {"error": "no refresh_token stored"}
        resp = self._post("/api/auth/refresh",
                          {"refresh_token": self._refresh_token}, auth=False)
        token = resp.get("access_token") or resp.get("token")
        if token:
            self._store_tokens(token, resp.get("refresh_token"))
        return resp

    def _token_expired(self, skew: int = 30) -> bool:
        """True if there's no token or its JWT exp is within ``skew`` seconds.

        Returns False for non-JWT/opaque tokens (can't tell — rely on 401 handling).
        """
        if not self._token:
            return True
        exp = _jwt_exp(self._token)
        if exp is None:
            return False
        import time
        return time.time() >= (exp - skew)

    def _ensure_token(self) -> None:
        """Ensure a usable access token: refresh first, then re-login with creds.

        Called both proactively (when the JWT is near expiry) and reactively (on a
        401). Tries the refresh_token grant first; if that is unavailable or leaves
        the token still expired, falls back to a fresh credential login.
        """
        if self._refresh_token:
            self.refresh()
        if self._token_expired() and self._email and self._password:
            self.login()

    # ── Key resolution ──────────────────────────────────────────────────────────

    def _fetch_encrypted_payload(self, path: str = "/api/users/me") -> str:
        """Fetch a raw encrypted ``payload`` string without decrypting it.

        Used to validate AES key candidates — needs a valid token but NOT the key.
        Bypasses _ensure_key()/_handle() to avoid recursion.
        """
        r = self.session.get(self.base_url + path)
        if r.status_code == 401 and self._can_reauth():
            self._ensure_token()
            r = self.session.get(self.base_url + path)
        r.raise_for_status()
        payload = r.json().get("payload")
        if not payload:
            raise RuntimeError(f"{path} did not return an encrypted payload to validate against")
        return payload

    def _ensure_key(self, sample_path: str = "/api/users/me") -> None:
        """Make sure the module-level AES key is set.

        Order: already-set / env / cache (handled at import) -> dynamic extraction
        from the deployed JS bundle, validated by round-trip decrypting a real
        response. The correct key is the only candidate that yields valid PKCS#7
        padding + parseable JSON.
        """
        if _KEY or self._resolving_key:
            return
        self._resolving_key = True
        try:
            from extract_key import find_key

            sample = self._fetch_encrypted_payload(sample_path)

            def validator(key_bytes: bytes) -> bool:
                try:
                    outer = json.loads(base64.b64decode(sample))
                    ct = base64.b64decode(outer["value"])
                    iv = base64.b64decode(outer["iv"])
                    pt = unpad(AES.new(key_bytes, AES.MODE_CBC, iv).decrypt(ct), AES.block_size)
                    json.loads(pt.decode("utf-8"))
                    return True
                except Exception:
                    return False

            key_b64 = find_key(self.base_url, validator=validator, session=self.session)
            set_key(key_b64)
        finally:
            self._resolving_key = False

    # ── Profile ───────────────────────────────────────────────────────────────

    def me(self) -> dict:
        """GET /api/users/me"""
        return self._get("/api/users/me")

    # ── Devices ───────────────────────────────────────────────────────────────

    def devices(self) -> dict:
        """GET /api/devices — list all devices."""
        return self._get("/api/devices")

    def device(self, device_id: str) -> dict:
        """GET /api/devices/<id>"""
        return self._get(f"/api/devices/{device_id}")

    def device_status(self, device_ids: list[str]) -> dict:
        """POST data-server /api/client/device/status — live status (form-encoded, unencrypted)."""
        # This endpoint lives on the data server and uses form-urlencoded, not JSON/encrypted
        body = "&".join(f"ids[{i}]={urllib.parse.quote(d)}" for i, d in enumerate(device_ids))
        r = self.session.post(
            f"{self._data_server}/api/client/device/status",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._handle_raw(r)

    def client_device(self) -> dict:
        return self._get("/api/client/device")

    def client_devices_priority(self) -> dict:
        return self._get("/api/client/devices/priority")

    def client_defaults(self) -> dict:
        return self._get("/api/client/defaults")

    # ── Rules ─────────────────────────────────────────────────────────────────

    def rules(self) -> dict:
        """GET /api/rules — list automation rules."""
        return self._get("/api/rules")

    # ── Analytics / Logs ──────────────────────────────────────────────────────

    def logs(self, params: dict | None = None) -> dict:
        """POST /api/logs — energy/telemetry log data."""
        return self._post("/api/logs", params or {})

    def storages(self) -> dict:
        return self._get("/api/storages")

    # ── Messages ──────────────────────────────────────────────────────────────

    def messages(self) -> dict:
        return self._get("/api/messages")

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def subscription(self, user_id: str) -> dict:
        return self._get(f"/api/subscriptions/personalSubscriptionData?user_id={user_id}")

    # ── Location / Geo ────────────────────────────────────────────────────────

    def geo_countries(self) -> dict:
        return self._get("/api/geo/countries")

    # ── Plans ─────────────────────────────────────────────────────────────────

    def plans(self) -> dict:
        return self._get("/api/plans")

    # ── Ping ──────────────────────────────────────────────────────────────────

    def ping(self) -> dict:
        return self._get("/api/ping")

    # ── Data-server endpoints (per-device, baseURL = device["server"]) ───────────
    # These calls go to https://data-au-1.energipays.com (NOT encrypted).

    def statistics(self, device_id: str, data_server: str | None = None,
                   tz: str = "Australia/Sydney", phase: str = "sum") -> dict:
        """POST data-server/api/client/device/{id}/statistics — aggregate totals."""
        base = (data_server or self._data_server).rstrip("/")
        r = self.session.post(
            f"{base}/api/client/device/{device_id}/statistics",
            data=urllib.parse.urlencode({"tz": tz, "phase": phase}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._handle_raw(r)

    def stats(self, device_id: str, date_from: str, date_to: str,
              data_server: str | None = None, tz: str = "Australia/Sydney",
              data_type: str = "power", phase: str = "sum") -> dict:
        """POST data-server/api/client/device/{id}/stats — per-day chart data.

        ``date_from`` / ``date_to`` format: "YYYY-MM-DD"
        """
        base = (data_server or self._data_server).rstrip("/")
        r = self.session.post(
            f"{base}/api/client/device/{device_id}/stats",
            data=urllib.parse.urlencode({
                "dateFrom": date_from, "dateTo": date_to,
                "tz": tz, "data_type": data_type, "phase": phase,
            }),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._handle_raw(r)

    def boost_device(self, device_id: str, data_server: str | None = None,
                     period: int = 1) -> dict:
        """POST data-server/api/client/device/{id}/boost — trigger a timed boost.

        ``period``: 1 = 1 hour, 2 = 2 hours, 3 = 3 hours (matches the app UI presets).
        Power level is controlled by the device's ``boost_power`` setting (set via the app).
        """
        base = (data_server or self._data_server).rstrip("/")
        r = self.session.post(
            f"{base}/api/client/device/{device_id}/boost",
            data=urllib.parse.urlencode({"period": period}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._handle_raw(r)

    def cancel_weather_boost(self, device_id: str, data_server: str | None = None) -> dict:
        """POST data-server/api/client/device/{id}/weather/boost/cancel"""
        base = (data_server or self._data_server).rstrip("/")
        r = self.session.post(
            f"{base}/api/client/device/{device_id}/weather/boost/cancel",
            data="", headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._handle_raw(r)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _can_reauth(self) -> bool:
        return bool(self._refresh_token) or bool(self._email and self._password)

    def _get(self, path: str, params: dict | None = None) -> dict:
        if self._token_expired():           # proactive: refresh before sending
            self._ensure_token()
        self._ensure_key()
        r = self.session.get(self.base_url + path, params=params)
        if r.status_code == 401 and self._can_reauth():   # reactive fallback
            self._ensure_token()
            r = self.session.get(self.base_url + path, params=params)
        return self._handle(r)

    def _post(self, path: str, body: dict, auth: bool = True) -> dict:
        if auth:
            if self._token_expired():
                self._ensure_token()
            self._ensure_key()
        payload = encrypt(json.dumps(body))
        # Encrypted POSTs go out as form-urlencoded `payload=<base64>` (confirmed
        # via HAR for /api/auth/login and /api/users/settings), NOT JSON.
        data = urllib.parse.urlencode({"payload": payload})
        # Unauthenticated requests (login/refresh) must NOT carry a stale/invalid
        # Bearer header — the server rejects any request with a bad token as 401
        # "Unauthenticated" before checking credentials. Strip it for auth=False.
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if not auth:
            headers["Authorization"] = None
        r = self.session.post(self.base_url + path, data=data, headers=headers)
        if r.status_code == 401 and auth and self._can_reauth():
            self._ensure_token()
            r = self.session.post(self.base_url + path, data=data,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self._handle(r)

    def _handle(self, r: requests.Response) -> dict:
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            try:
                outer = r.json()
                if "payload" in outer:
                    return {"error_decrypted": decrypt(outer["payload"]),
                            "status": r.status_code}
            except Exception:
                pass
            return {"error": str(e), "status": r.status_code, "body": r.text[:500]}
        try:
            outer = r.json()
            if "payload" in outer:
                return decrypt(outer["payload"])
            return outer
        except Exception:
            return {"raw": r.text}

    def _handle_raw(self, r: requests.Response) -> dict:
        """Handle unencrypted responses (data server)."""
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            return {"error": str(e), "status": r.status_code, "body": r.text[:500]}
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}


# ── CLI probe ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import getpass, sys

    if _KEY:
        print(f"→ AES key loaded ({'env' if os.environ.get('ENERGIPAYS_KEY') else 'cache'})")
    else:
        print("→ no AES key set — will auto-extract from JS bundle after auth")

    # Credentials: env vars first, prompt only if missing.
    email = _EMAIL or input("Email: ").strip()
    password = _PASSWORD or getpass.getpass("Password: ")

    client = EnergipaysClient(email=email, password=password, auto_login=False)
    print("\n→ /api/users/login")
    login_resp = client.login()
    # Check the login RESPONSE for a token — not client._token, which may be a
    # stale token preloaded from .token_cache.json and would mask a failed login.
    login_token = (login_resp.get("access_token") or login_resp.get("token")
                   or login_resp.get("accessToken")) if isinstance(login_resp, dict) else None
    if not login_token:
        print(json.dumps(login_resp, indent=2))
        print("Login failed.", file=sys.stderr)
        sys.exit(1)
    print(f"→ authenticated ({client._token[:40]}...)")

    # Pull device list first so we can show per-device telemetry
    print("\n→ /api/devices")
    devs_resp = client.devices()
    print(json.dumps(devs_resp, indent=2))

    devices_list = devs_resp if isinstance(devs_resp, list) else devs_resp.get("data", [])

    for dev in devices_list:
        dev_id = dev.get("id", "")
        data_server = dev.get("server", "")
        name = dev.get("name", dev_id)
        print(f"\n→ statistics: {name}")
        try:
            print(json.dumps(client.statistics(dev_id, data_server), indent=2))
        except Exception as e:
            print(f"  error: {e}")
        print(f"\n→ device_status: {name}")
        try:
            print(json.dumps(client.device_status([dev_id]), indent=2))
        except Exception as e:
            print(f"  error: {e}")

    for label, fn in [
        ("me",    client.me),
        ("rules", client.rules),
        ("logs",  lambda: client.logs({"limit": 5})),
    ]:
        print(f"\n→ {label}")
        try:
            print(json.dumps(fn(), indent=2))
        except Exception as e:
            print(f"  error: {e}")
