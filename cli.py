#!/usr/bin/env python3
"""
Command-line client for the Energipays API.

Credentials are taken from (in order): command-line flags, environment variables
(ENERGIPAYS_EMAIL / ENERGIPAYS_PASSWORD), or an interactive prompt.

Examples:
  python cli.py --email you@example.com --password secret me
  ENERGIPAYS_EMAIL=you@example.com ENERGIPAYS_PASSWORD=secret python cli.py devices
  python cli.py -e you@example.com --password-stdin me   # read password from stdin
  python cli.py stats d2784559-... --from 2026-06-01 --to 2026-06-24
  python cli.py key                                       # show the resolved AES key

Security note: passing --password on the command line can expose it via shell
history and the process list (ps). Prefer env vars or --password-stdin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from energipays import EnergipaysClient


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _resolve_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    if args.password:
        return args.password
    env = os.environ.get("ENERGIPAYS_PASSWORD", "")
    if env:
        return env
    return getpass.getpass("Password: ")


def _make_client(args) -> EnergipaysClient:
    email = args.email or os.environ.get("ENERGIPAYS_EMAIL", "") or input("Email: ").strip()
    password = _resolve_password(args)
    client = EnergipaysClient(
        email=email, password=password, base_url=args.base_url, auto_login=False
    )
    resp = client.login()
    # Check the login RESPONSE for a token — not client._token, which may hold a
    # stale token preloaded from .token_cache.json and mask a failed login.
    token = resp.get("access_token") or resp.get("token") or resp.get("accessToken")
    if not token:
        _out(resp)
        sys.exit("Login failed — check your email/password (or the account may use SSO).")
    return client


def _first_device(client: EnergipaysClient) -> tuple[str, str]:
    """Return (device_id, data_server) for the first device. Exits if none found."""
    resp = client.devices()
    devices = resp if isinstance(resp, list) else resp.get("data", [])
    if not devices:
        sys.exit("No devices found on this account.")
    dev = devices[0]
    return dev["id"], dev.get("server", "https://data-au-1.energipays.com")


# ── Subcommand handlers ─────────────────────────────────────────────────────────

def cmd_me(c, a):            return c.me()
def cmd_devices(c, a):       return c.devices()
def cmd_rules(c, a):         return c.rules()
def cmd_logs(c, a):          return c.logs({"limit": a.limit} if a.limit else {})

def cmd_device(c, a):
    dev_id = a.device_id or _first_device(c)[0]
    return c.device(dev_id)

def cmd_status(c, a):
    ids = a.device_ids or [_first_device(c)[0]]
    return c.device_status(ids)

def cmd_statistics(c, a):
    dev_id, server = (a.device_id, a.server) if a.device_id else _first_device(c)
    return c.statistics(dev_id, a.server or server, tz=a.tz, phase=a.phase)

def cmd_stats(c, a):
    dev_id, server = (a.device_id, a.server) if a.device_id else _first_device(c)
    return c.stats(dev_id, a.date_from, a.date_to,
                   a.server or server, tz=a.tz, data_type=a.data_type, phase=a.phase)

def cmd_boost(c, a):
    if not a.yes:
        sys.exit("Refusing to send a boost command without --yes (this controls a device).")
    dev_id, server = (a.device_id, a.server) if a.device_id else _first_device(c)
    return c.boost_device(dev_id, a.server or server, period=a.period)

def cmd_device_on(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    dev_id = a.device_id or _first_device(c)[0]
    return c.set_device_status(dev_id, status=1)

def cmd_device_off(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    dev_id = a.device_id or _first_device(c)[0]
    return c.set_device_status(dev_id, status=0)

def cmd_heater_on(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    dev_id = a.device_id or _first_device(c)[0]
    return c.set_device_status(dev_id, heaterStatus=1)

def cmd_heater_off(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    dev_id = a.device_id or _first_device(c)[0]
    return c.set_device_status(dev_id, heaterStatus=0)


def cmd_key(c, a):
    """Resolve and print the AES key (triggers extraction if needed)."""
    import energipays as ep
    c._ensure_key()
    import base64
    return {"key_b64": base64.b64encode(ep._KEY).decode() if ep._KEY else None}


# ── Parser ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="energipays",
        description="Standalone command-line client for the Energipays API.",
        epilog="Credentials: --email/--password, else ENERGIPAYS_EMAIL/ENERGIPAYS_PASSWORD, else prompt.",
    )
    p.add_argument("-e", "--email", help="account email (or set ENERGIPAYS_EMAIL)")
    p.add_argument("-p", "--password", help="account password (insecure; prefer env or --password-stdin)")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.add_argument("--base-url", default="https://energipays.com", help="API base URL")

    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name, handler, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=handler)
        return sp

    add("me", cmd_me, "show the authenticated user profile")
    add("devices", cmd_devices, "list all devices")
    add("rules", cmd_rules, "list automation rules")
    add("key", cmd_key, "resolve and print the AES key (base64)")

    sp = add("device", cmd_device, "show a single device (defaults to first device)")
    sp.add_argument("device_id", nargs="?", default=None)

    sp = add("logs", cmd_logs, "fetch energy/telemetry logs")
    sp.add_argument("--limit", type=int, default=5)

    sp = add("status", cmd_status, "live device status (defaults to first device)")
    sp.add_argument("device_ids", nargs="*", metavar="device_id")

    sp = add("statistics", cmd_statistics, "aggregate totals (defaults to first device)")
    sp.add_argument("device_id", nargs="?", default=None)
    sp.add_argument("--server", help="override the device data server URL")
    sp.add_argument("--tz", default="Australia/Sydney")
    sp.add_argument("--phase", default="sum")

    sp = add("stats", cmd_stats, "per-day chart data (defaults to first device)")
    sp.add_argument("device_id", nargs="?", default=None)
    sp.add_argument("--from", dest="date_from", required=True, metavar="YYYY-MM-DD")
    sp.add_argument("--to", dest="date_to", required=True, metavar="YYYY-MM-DD")
    sp.add_argument("--server", help="override the device data server URL")
    sp.add_argument("--tz", default="Australia/Sydney")
    sp.add_argument("--data-type", dest="data_type", default="power")
    sp.add_argument("--phase", default="sum")

    sp = add("boost", cmd_boost, "trigger a timed boost (defaults to first device)")
    sp.add_argument("device_id", nargs="?", default=None)
    sp.add_argument("--period", type=int, default=1, choices=[1, 2, 3],
                    help="boost duration: 1=1h, 2=2h, 3=3h (default: 1h)")
    sp.add_argument("--server", help="override the device data server URL")
    sp.add_argument("--yes", action="store_true", help="confirm sending the device command")

    for name, handler, help_text in [
        ("device-on",   cmd_device_on,  "enable the PowerDiverter (defaults to first device)"),
        ("device-off",  cmd_device_off, "disable the PowerDiverter (defaults to first device)"),
        ("heater-on",   cmd_heater_on,  "turn the immersion heater on (defaults to first device)"),
        ("heater-off",  cmd_heater_off, "turn the immersion heater off (defaults to first device)"),
    ]:
        sp = add(name, handler, help_text)
        sp.add_argument("device_id", nargs="?", default=None)
        sp.add_argument("--yes", action="store_true", help="confirm sending the device command")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = _make_client(args)
    _out(args.func(client, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
