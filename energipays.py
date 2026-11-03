"""
Energipays API client — standalone, credential-based.

Authentication: email/password login (POST /api/auth/login) which returns a
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
import logging
import os
import pathlib
import secrets
import threading
import time
import urllib.parse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

log = logging.getLogger(__name__)

BASE_URL = "https://energipays.com"

# Schedule command constants (customer_rule.d1..d7 time-slot command field)
CMD_IDLE  = "1"   # standby / idle
CMD_BOOST = "2"   # boost (uses boost_power / temperature_min)

# Boost ``period`` values sent to the data server's /boost endpoint.
# energipays.com bundle (boostDevice + the boost buttons): 1 h -> 2, 2 h -> 3, 3 h -> 4,
# and cancel sends period 1 (there is no cancel endpoint). energipays-mobile labels
# period 1 as 30 min; the site never offers it as a duration.
BOOST_PERIOD_CANCEL = 1
BOOST_PERIODS = {1: 2, 2: 3, 3: 4}  # hours -> period
BOOST_PERIOD_LABELS = {1: "30 min / cancel", 2: "1 hour", 3: "2 hours", 4: "3 hours"}

# Circuit (rule ``type``) -> the status_data field holding that circuit's active rule.
# energipays.com bundle: updateDevice + the rule handlers.
RULE_FIELDS = {"command": "rule_customer", "offpeak": "rule_offpeak", "heater2": "rule_heater2"}
_NO_GROUP = {"", "0", "empty", "null", "none", "unknown"}

# Seconds energipays.com waits before retrying a data-server call that returned 401.
DATA_RETRY_DELAY = 1.0

# AES-256-CBC key. Resolution order (see EnergipaysClient._ensure_key):
#   1. ENERGIPAYS_KEY env var
#   2. .key_cache.json (written by a previous dynamic extraction)
#   3. dynamic extraction from the deployed JS bundle (extract_key.find_key),
#      validated by round-trip decrypting a live response.
# The key is NOT hardcoded — it lives in client-side JS and rotates on each deploy.
# Cache location: DATA_DIR env var (data volume in Docker) > script parent dir.
_KEY_CACHE = (
    pathlib.Path(os.environ["DATA_DIR"]) / ".key_cache.json"
    if os.environ.get("DATA_DIR")
    else pathlib.Path(__file__).parent / ".key_cache.json"
)


def _load_key_cache() -> str:
    if _KEY_CACHE.exists():
        try:
            return json.loads(_KEY_CACHE.read_text()).get("key", "")
        except Exception:
            pass
    return ""


_KEY_B64 = os.environ.get("ENERGIPAYS_KEY", "") or _load_key_cache()
_KEY = base64.b64decode(_KEY_B64) if _KEY_B64 else b""

if _KEY_B64:
    _src = "ENERGIPAYS_KEY env var" if os.environ.get("ENERGIPAYS_KEY") else ".key_cache.json"
    log.debug("crypto: AES wire key loaded at import from %s (%d bytes)", _src, len(_KEY))


def set_key(b64_key: str) -> None:
    """Set the AES key at runtime (alternative to ENERGIPAYS_KEY env var)."""
    global _KEY
    _KEY = base64.b64decode(b64_key)
    log.info("crypto: AES-256-CBC wire key set (%d bytes)", len(_KEY))


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


def mask_email(email: str | None) -> str:
    """``user@example.com`` -> ``u•••r@example.com``: for logs, which get shared."""
    if not email or "@" not in email:
        return "•••" if email else ""
    local, _, domain = email.partition("@")
    shown = local[0] + "•••" + local[-1] if len(local) > 2 else local[:1] + "•••"
    return f"{shown}@{domain}"


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
        # Serialises refresh/login: callers such as the bridge use the client from
        # several threads, and two concurrent refreshes would race on the token.
        self._auth_lock = threading.RLock()
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
            log.debug("auth: tokens cached to %s", _TOKEN_CACHE)
        except Exception:
            pass

    def _ensure_csrf(self) -> None:
        """Obtain the Laravel XSRF-TOKEN + session cookie and set X-XSRF-TOKEN.

        Login uses Sanctum session/CSRF protection: the server rejects the POST as
        "Unauthenticated" unless the request carries the session cookie and a
        matching X-XSRF-TOKEN header. We fetch these programmatically (no browser).
        """
        log.debug("auth: fetching Laravel Sanctum CSRF cookie")
        for path in ("/sanctum/csrf-cookie", "/"):
            try:
                self.session.get(self.base_url + path, timeout=15)
            except Exception:
                continue
            xsrf = self.session.cookies.get("XSRF-TOKEN")
            if xsrf:
                # Browser sends the URL-decoded cookie value as the header.
                self.session.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)
                log.debug("auth: CSRF token acquired via %s — X-XSRF-TOKEN set", path)
                return
        log.warning("auth: could not obtain CSRF token — login may be rejected")

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
        log.info("auth: login sequence start for %s", mask_email(email))
        self._ensure_key()
        log.debug("auth: AES wire key ready — encrypting credentials for POST")
        self._ensure_csrf()
        payload = encrypt(json.dumps({"username": email, "password": password}))
        log.debug("auth: sending AES-256-CBC encrypted login POST to %s/api/auth/login", self.base_url)
        r = self.session.post(
            self.base_url + "/api/auth/login",
            data=urllib.parse.urlencode({"payload": payload}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = self._handle(r)
        token = resp.get("access_token") or resp.get("token") or resp.get("accessToken")
        if token:
            exp = _jwt_exp(token)
            exp_info = f"expires at unix {exp}" if exp else "no exp (opaque token)"
            log.info("auth: Bearer token received (%s)", exp_info)
            self._store_tokens(token, resp.get("refresh_token"))
        else:
            log.warning("auth: login response contained no token")
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
        return time.time() >= (exp - skew)

    def _ensure_token(self) -> None:
        """Ensure a usable access token: refresh first, then re-login with creds.

        Called both proactively (when the JWT is near expiry) and reactively (on a
        401). Tries the refresh_token grant first; if that is unavailable or leaves
        the token still expired, falls back to a fresh credential login.
        """
        with self._auth_lock:
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
        r = self.session.get(self.base_url + path, timeout=20)
        if r.status_code == 401 and self._can_reauth():
            self._ensure_token()
            r = self.session.get(self.base_url + path, timeout=20)
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
        if _KEY:
            log.debug("crypto: AES wire key already loaded — skipping extraction")
            return
        if self._resolving_key:
            return
        self._resolving_key = True
        try:
            log.info("crypto: AES wire key not set — starting dynamic extraction from JS bundle")
            from extract_key import context_candidates_from_js, discover_chunk_urls, find_key

            # Fast path: scan JS bundle for Base64.parse("...") candidates without
            # needing a live token. If exactly one high-signal candidate is found
            # across all chunks, use it directly — avoids a chicken-and-egg
            # dependency on login.
            fast_key: str | None = None
            fast_err = ""
            try:
                chunk_urls = discover_chunk_urls(self.base_url, self.session)
                candidates: list[str] = []
                for url in chunk_urls:
                    r = self.session.get(url, timeout=20)
                    if r.ok:
                        for c in context_candidates_from_js(r.text):
                            if c not in candidates:
                                candidates.append(c)
                if len(candidates) == 1:
                    fast_key = candidates[0]
                elif not chunk_urls:
                    fast_err = "no JS chunk URLs discovered at " + self.base_url
                else:
                    fast_err = (f"{len(candidates)} Base64.parse candidates across "
                                f"{len(chunk_urls)} chunks (need exactly 1)")
            except Exception as _fe:
                fast_err = f"{type(_fe).__name__}: {_fe}"

            if fast_key:
                log.info("crypto: AES key extracted from JS bundle (single Base64.parse candidate) — no validator needed")
                # Save to cache so subsequent restarts skip extraction entirely
                try:
                    import json as _j
                    _KEY_CACHE.write_text(_j.dumps({"key": fast_key}))
                except Exception:
                    pass
                set_key(fast_key)
            else:
                log.warning("crypto: fast-path key extraction failed — %s", fast_err)
                # Validated path: needs a live Bearer token to fetch an encrypted
                # sample response. Without one it MUST NOT proceed: the sample
                # fetch would 401 -> _ensure_token() -> login() -> _ensure_key()
                # (blocked by the re-entry guard) -> encrypt() dies with the
                # generic "AES key not set". Fail loud and actionable instead.
                if not self._token or self._token_expired():
                    raise RuntimeError(
                        "Could not auto-extract the AES key from the "
                        f"{self.base_url} JS bundle ({fast_err}), and no valid "
                        "login token exists to validate key candidates against. "
                        "Set the key manually via the ENERGIPAYS_KEY env var, "
                        "the add-on Configuration -> AES Key field, or the setup "
                        "wizard's key diagnostics."
                    )
                log.debug("crypto: fetching encrypted sample payload from %s for key validation", sample_path)
                sample = self._fetch_encrypted_payload(sample_path)
                log.debug("crypto: sample payload acquired (%d chars) — scanning JS bundle for key candidates", len(sample))

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
                log.info("crypto: AES key extracted and validated — decryption round-trip confirmed OK")
                set_key(key_b64)
        except Exception as exc:
            log.error("crypto: key extraction failed — %s", exc)
            raise
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
        return self._data_post(f"{self._data_server}/api/client/device/status", body)

    def client_device(self) -> dict:
        return self._get("/api/client/device")

    def client_devices_priority(self) -> dict:
        return self._get("/api/client/devices/priority")

    def client_defaults(self) -> dict:
        return self._get("/api/client/defaults")

    def set_device_status(self, device_id: str, **fields) -> dict:
        """POST /api/devices/{id}/status — toggle individual device switches.

        Confirmed fields (from HAR 2026-06-24):
          status=1/0                     — PowerDiverter on/off
          heaterStatus=1/0               — immersion heater on/off
          offpeakSwitcherStatus=1/0      — off-peak tariff mode on/off
          (and other switchers from the full status_data object)

        All encrypted, main API (energipays.com).
        """
        return self._post(f"/api/devices/{device_id}/status", fields)

    def activate_rule(self, device_id: str, rule_id: str, circuit: str = "command") -> dict:
        """Activate a rule on one circuit, or clear it with ``rule_id="0"``.

        ``circuit``: ``"command"`` (PD), ``"offpeak"`` (Off-Peak), ``"heater2"`` (Heater 2).

        Mirrors energipays.com (bundle: updateDevice + the rule handlers): read the
        device, take its current ``status_data``, change only the circuit's
        ``rule_customer`` / ``rule_offpeak`` / ``rule_heater2`` field, and
        ``PUT /api/devices/{id}`` with ``{status_data, rule_id, type}``. No other
        status_data field (``admin``, ``customer``, ``retailer``, switches) is changed.

        The short body ``{rule_id, type}`` returned HTTP 500 on the live cloud
        (energipays-mobile, 2026-11-03; issue #2), so it is never sent.

        Returns the server response, an ``{"error": ...}`` dict, or
        ``{"skipped": ...}`` when the device is in a group (the site does nothing
        then: rules are managed by the group).
        """
        field = RULE_FIELDS.get(circuit)
        if field is None:
            raise ValueError(f"unknown circuit {circuit!r}; expected one of {sorted(RULE_FIELDS)}")
        prof = self.device(device_id)
        if isinstance(prof, dict) and ("error" in prof or "error_decrypted" in prof):
            return prof
        if isinstance(prof, dict) and isinstance(prof.get("data"), dict):
            prof = prof["data"]
        if not isinstance(prof, dict) or not isinstance(prof.get("status_data"), dict):
            return {"error": "device has no status_data; rule not sent", "status": None}
        group = str(prof.get("group_id") or "").strip().lower()
        if group not in _NO_GROUP:
            return {"skipped": "device is in a group; its rules are managed by the group",
                    "group_id": prof.get("group_id")}
        status_data = dict(prof["status_data"])
        status_data[field] = rule_id
        log.info("rules: activating rule %s on circuit %s", rule_id, circuit)
        return self.update_device(device_id, status_data, rule_id, rule_type=circuit)

    def set_rule(self, device_id: str, rule_id: str, rule_type: str = "command") -> dict:
        """Deprecated alias of :meth:`activate_rule` (kept for existing callers).

        It used to send the short body ``{rule_id, type}``, which the cloud rejects
        with HTTP 500 (issue #2). It now does what energipays.com does.
        """
        return self.activate_rule(device_id, rule_id, rule_type)

    def update_device(self, device_id: str, status_data: dict,
                      rule_id: str = "0", rule_type: str = "command") -> dict:
        """PUT /api/devices/{id} with ``{status_data, rule_id, type}`` (encrypted).

        ``status_data`` must be the device's complete current status_data with only
        the intended fields changed: the server stores what it is sent. Prefer
        :meth:`activate_rule`, which builds it. ``rule_type`` is the circuit:
        ``"command"``, ``"offpeak"`` or ``"heater2"``.
        """
        return self._put(f"/api/devices/{device_id}",
                         {"status_data": status_data, "rule_id": rule_id, "type": rule_type})

    # ── Rules ─────────────────────────────────────────────────────────────────

    def rules(self) -> dict:
        """GET /api/rules — list automation rules."""
        return self._get("/api/rules")

    def create_rule(self, name: str, rule_type: str = "command") -> dict:
        """POST /api/rules — create a new automation rule (encrypted)."""
        log.info("rules: creating rule '%s' (type=%s)", name, rule_type)
        return self._post("/api/rules", {"name": name, "type": rule_type, "data": {}})

    def update_rule(self, rule_id: str, rule_data: dict) -> dict:
        """PUT /api/rules/{id} — update an automation rule (encrypted).

        The server only applies changes when the body is exactly {"data": {dN: [slots]}}.
        Sending the full rule object causes the server to silently ignore the data field.
        """
        log.info("rules: updating rule %s (%s)", rule_id, rule_data.get("name", ""))
        body: dict = {"data": rule_data.get("data", {})}
        return self._put(f"/api/rules/{rule_id}", body)

    def rename_rule(self, rule_id: str, name: str) -> dict:
        """PUT /api/rules/{id} — rename a rule (sends only {"name": ...})."""
        log.info("rules: renaming rule %s to %s", rule_id, name)
        return self._put(f"/api/rules/{rule_id}", {"name": name})

    def delete_rule(self, rule_id: str) -> dict:
        """DELETE /api/rules/{id} — remove an automation rule (encrypted)."""
        log.info("rules: deleting rule %s", rule_id)
        return self._delete(f"/api/rules/{rule_id}")

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
        """POST data-server/api/client/device/{id}/statistics — aggregate totals.

        Returns ``today``, ``yesterday``, ``week`` and ``fortnight`` buckets; the
        site's "Last 14 days" is ``fortnight``. ``phase`` is ``sum``, ``a``, ``b``
        or ``c`` (energipays.com bundle, statistics call).
        """
        base = (data_server or self._data_server).rstrip("/")
        return self._data_post(f"{base}/api/client/device/{device_id}/statistics", urllib.parse.urlencode({"tz": tz, "phase": phase}))

    def stats(self, device_id: str, date_from: str, date_to: str,
              data_server: str | None = None, tz: str = "Australia/Sydney",
              data_type: str = "power", phase: str = "sum") -> dict:
        """POST data-server/api/client/device/{id}/stats — per-day chart data.

        ``date_from`` / ``date_to`` format: "YYYY-MM-DD".
        ``data_type``: ``"consumption"`` is what the site's Energy view sends,
        ``"power"`` its Power view. ``phase``: ``sum``, ``a``, ``b`` or ``c``.
        Grid sign (site): ``phasePower > 0`` is export; in the power series
        ``EIP < 0`` is import.
        """
        base = (data_server or self._data_server).rstrip("/")
        return self._data_post(f"{base}/api/client/device/{device_id}/stats", urllib.parse.urlencode({
                "dateFrom": date_from, "dateTo": date_to,
                "tz": tz, "data_type": data_type, "phase": phase,
            }))

    def set_boost_power(self, device_id: str, power: int) -> dict:
        """POST /api/devices/{id}/boostPower — set boost power level (encrypted, main API).

        ``power``: 1=25%, 2=50%, 3=75%, 4=100% (matches the app UI presets).
        Confirmed endpoint from HAR 2026-06-26.
        """
        return self._post(f"/api/devices/{device_id}/boostPower", {"boost_power": power})

    def boost_device(self, device_id: str, data_server: str | None = None,
                     period: int = BOOST_PERIODS[1]) -> dict:
        """POST data-server/api/client/device/{id}/boost — trigger a timed boost.

        ``period`` is the raw value energipays.com sends: 2 = 1 hour, 3 = 2 hours,
        4 = 3 hours (``BOOST_PERIODS`` maps hours to it). 1 is what the site sends to
        cancel; use :meth:`cancel_boost` for that. Power level is the device's
        ``boost_power`` setting (:meth:`set_boost_power`).

        A 202 response means the command was queued for the device, not applied:
        the result then carries ``"queued": True``.
        """
        if period not in BOOST_PERIOD_LABELS:
            raise ValueError(f"period must be one of {sorted(BOOST_PERIOD_LABELS)}, got {period!r}")
        base = (data_server or self._data_server).rstrip("/")
        return self._data_post(f"{base}/api/client/device/{device_id}/boost", urllib.parse.urlencode({"period": period}))

    def cancel_boost(self, device_id: str, data_server: str | None = None) -> dict:
        """Cancel a manual boost.

        HAR-confirmed (2026-06-26): energipays.com sends POST /boost with period=1
        to cancel — there is no separate cancel endpoint.
        """
        base = (data_server or self._data_server).rstrip("/")
        return self._data_post(f"{base}/api/client/device/{device_id}/boost", urllib.parse.urlencode({"period": BOOST_PERIOD_CANCEL}))

    def cancel_weather_boost(self, device_id: str, data_server: str | None = None) -> dict:
        """POST data-server/api/client/device/{id}/weather/boost/cancel"""
        base = (data_server or self._data_server).rstrip("/")
        return self._data_post(f"{base}/api/client/device/{device_id}/weather/boost/cancel", "")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _data_post(self, url: str, data: str) -> dict:
        """POST form data to a data server (unencrypted, Bearer token).

        Same token handling as the main host (issue #3): refresh first when the JWT
        is near expiry, and on a 401 refresh and retry once. energipays.com's
        data-server helper also retries once after a 401, after a 1 s delay.
        """
        if self._token_expired():
            self._ensure_token()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        r = self.session.post(url, data=data, headers=headers)
        if r.status_code == 401 and self._can_reauth():
            time.sleep(DATA_RETRY_DELAY)
            self._ensure_token()
            r = self.session.post(url, data=data, headers=headers)
        return self._handle_raw(r)

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

    def _put(self, path: str, body: dict) -> dict:
        if self._token_expired():
            self._ensure_token()
        self._ensure_key()
        payload = encrypt(json.dumps(body))
        data = urllib.parse.urlencode({"payload": payload})
        r = self.session.put(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code == 401 and self._can_reauth():
            self._ensure_token()
            r = self.session.put(
                self.base_url + path, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return self._handle(r)

    def _delete(self, path: str) -> dict:
        if self._token_expired():
            self._ensure_token()
        r = self.session.delete(self.base_url + path)
        if r.status_code == 401 and self._can_reauth():
            self._ensure_token()
            r = self.session.delete(self.base_url + path)
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
        """Handle unencrypted responses (data server).

        HTTP 202 means the device command was queued, not applied (energipays.com
        ``queueHandler``); it is returned as ``{"queued": True, "status": 202, ...}``
        so callers can tell it from a plain success.
        """
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            return {"error": str(e), "status": r.status_code, "body": r.text[:500]}
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        if r.status_code == 202:
            return {"queued": True, "status": 202, "body": body}
        return body


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
    print("\n→ /api/auth/login")
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
