"""
Energipays API client — reverse-engineered from main.9860a158.chunk.js

Encryption: AES-256-CBC via CryptoJS.
Wire format (both directions): base64( JSON({ value, iv, mac }) )
  value = base64(AES-CBC ciphertext)
  iv    = base64(random 16-byte IV)
  mac   = HMAC-SHA256(key, iv_b64 + value_b64).hexdigest()

Set ENERGIPAYS_KEY env var to the base64-encoded 32-byte AES key extracted
from the JS bundle (key found in main.*.chunk.js, variable A in CryptoJS block).
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

# AES-256-CBC key — loaded from env var. Extract from main.*.chunk.js:
#   search for: y.a.enc.Base64.parse("...")  (the first 44-char base64 string near CryptoJS)
_KEY_B64 = os.environ.get("ENERGIPAYS_KEY", "")
_KEY = base64.b64decode(_KEY_B64) if _KEY_B64 else b""


def set_key(b64_key: str) -> None:
    """Set the AES key at runtime (alternative to ENERGIPAYS_KEY env var)."""
    global _KEY
    _KEY = base64.b64decode(b64_key)


# ── Token bootstrap ───────────────────────────────────────────────────────────
# If login fails (e.g. account uses SSO), extract the JWT from the browser:
#   Object.entries(localStorage).forEach(([k,v])=>console.log(k,v))
# then pass it via ENERGIPAYS_TOKEN env var or set_token().

# Token bootstrap — tries env var first, then local token cache written by token_server.py
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

# ENERGIPAYS_TOKEN may be the raw JWT or the full JSON blob from localStorage.tokens
_raw_token_env = os.environ.get("ENERGIPAYS_TOKEN", "")
if _raw_token_env.startswith("{"):
    _BOOTSTRAP_TOKEN = json.loads(_raw_token_env).get("access_token", "")
else:
    _BOOTSTRAP_TOKEN = _raw_token_env

_cache_access, _cache_refresh = _load_token_cache()
if not _BOOTSTRAP_TOKEN and _cache_access:
    _BOOTSTRAP_TOKEN = _cache_access

_BOOTSTRAP_REFRESH = os.environ.get("ENERGIPAYS_REFRESH_TOKEN", "") or _cache_refresh
# URL-encoded value from document.cookie — we URL-decode it for the X-XSRF-TOKEN header
_XSRF_RAW = os.environ.get("ENERGIPAYS_XSRF_TOKEN", "")
_XSRF_TOKEN = urllib.parse.unquote(_XSRF_RAW) if _XSRF_RAW else ""
_SESSION_RAW = os.environ.get("ENERGIPAYS_SESSION", "")


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


# ── Client ────────────────────────────────────────────────────────────────────

class EnergipaysClient:
    """Authenticated Energipays API client."""

    def __init__(self, base_url: str = BASE_URL, data_server: str = "https://data-au-1.energipays.com"):
        self.base_url = base_url.rstrip("/")
        self._data_server = data_server.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": base_url,
            "Referer": base_url + "/my-devices",
        })
        if _XSRF_TOKEN:
            self.session.headers["X-XSRF-TOKEN"] = _XSRF_TOKEN
            self.session.cookies.set("XSRF-TOKEN", _XSRF_RAW, domain="energipays.com")
        if _SESSION_RAW:
            self.session.cookies.set("prod_main_session", _SESSION_RAW, domain="energipays.com")
        self._token: str | None = None
        self._refresh_token: str | None = _BOOTSTRAP_REFRESH or None
        if _BOOTSTRAP_TOKEN:
            # Use supplied access token directly; refresh on 401 if refresh token available
            self._token = _BOOTSTRAP_TOKEN
            self.session.headers["Authorization"] = f"Bearer {_BOOTSTRAP_TOKEN}"
        elif _BOOTSTRAP_REFRESH:
            # No access token — refresh immediately to get one
            self.refresh()

    @classmethod
    def from_chrome(cls, **kwargs) -> "EnergipaysClient":
        """Create a client by pulling a fresh token from the logged-in Chrome tab via CDP."""
        from cdp_tokens import get_tokens
        tokens = get_tokens(save=True)
        access = tokens.get("access_token", "")
        refresh = tokens.get("refresh_token", "")
        if not access:
            raise RuntimeError("No access_token in Chrome localStorage — is the tab logged in?")
        import os
        os.environ.setdefault("ENERGIPAYS_KEY", "w0oBD4tKUu8EqNF0sjiqxGwlkv92ZUXV0cBSjdwLqwA=")
        client = cls(**kwargs)
        client._token = access
        client._refresh_token = refresh
        client.session.headers["Authorization"] = f"Bearer {access}"
        return client

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> dict:
        """POST /api/users/login — customer login; stores Bearer token."""
        resp = self._post("/api/users/login", {"username": email, "password": password}, auth=False)
        token = resp.get("access_token") or resp.get("token") or resp.get("accessToken")
        if token:
            self._token = token
            self.session.headers["Authorization"] = f"Bearer {token}"
        return resp

    def refresh(self) -> dict:
        """POST /api/auth/refresh — exchange refresh_token for a new access_token."""
        if not self._refresh_token:
            return {"error": "no refresh_token stored"}
        resp = self._post("/api/auth/refresh",
                          {"refresh_token": self._refresh_token}, auth=False)
        token = resp.get("access_token") or resp.get("token")
        if token:
            self._token = token
            self.session.headers["Authorization"] = f"Bearer {token}"
            if resp.get("refresh_token"):
                self._refresh_token = resp["refresh_token"]
            # Persist refreshed tokens so next run doesn't need a new paste
            try:
                _TOKEN_CACHE.write_text(json.dumps({
                    "access_token": token,
                    "refresh_token": self._refresh_token or "",
                }))
            except Exception:
                pass
        return resp

    def _ensure_token(self) -> None:
        """Auto-refresh if we have a refresh token but the access token was rejected."""
        if self._refresh_token:
            self.refresh()

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
                     period: int = 60) -> dict:
        """POST data-server/api/client/device/{id}/boost — trigger a timed boost.

        ``period`` in minutes.
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

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(self.base_url + path, params=params)
        if r.status_code == 401 and self._refresh_token:
            self._ensure_token()
            r = self.session.get(self.base_url + path, params=params)
        return self._handle(r)

    def _post(self, path: str, body: dict, auth: bool = True) -> dict:
        payload = encrypt(json.dumps(body))
        r = self.session.post(self.base_url + path, json={"payload": payload})
        if r.status_code == 401 and auth and self._refresh_token:
            self._ensure_token()
            r = self.session.post(self.base_url + path, json={"payload": payload})
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

    aes_key = os.environ.get("ENERGIPAYS_KEY")
    if not aes_key:
        aes_key = input("AES key (base64, from JS bundle): ").strip()
        set_key(aes_key)

    client = EnergipaysClient()

    if _BOOTSTRAP_TOKEN:
        print(f"→ using access token ({_BOOTSTRAP_TOKEN[:40]}...)")
    elif _BOOTSTRAP_REFRESH:
        if not client._token:
            print("Token refresh failed — get a fresh refresh_token from browser", file=sys.stderr)
            sys.exit(1)
        print(f"→ refreshed token ({client._token[:40]}...)")
    elif _SESSION_RAW:
        print("→ using session cookie (no Bearer token)")
    else:
        email = input("Email: ").strip()
        password = getpass.getpass("Password: ")
        print("\n→ /api/users/login")
        login_resp = client.login(email, password)
        print(json.dumps(login_resp, indent=2))
        if not client._token:
            print("Login failed.", file=sys.stderr)
            sys.exit(1)

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
