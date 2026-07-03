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
  python cli.py boost --period 2 --yes                   # 1=30min 2=1h(default) 3=2h
  python cli.py boost --period 2 --power 75 --yes        # set power + boost in one step
  python cli.py cancel --yes                             # cancel running boost
  python cli.py power --percent 75 --yes                 # set boost power without boosting
  python cli.py device-on --yes / device-off --yes       # enable/disable PowerDiverter
  python cli.py heater-on --yes / heater-off --yes       # control immersion heater
  python cli.py key                                      # show the resolved AES key
  python cli.py status --json                            # raw JSON dump

Security note: passing --password on the command line can expose it via shell
history and the process list (ps). Prefer env vars or --password-stdin.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import sys

from energipays import EnergipaysClient


# ── Output helpers ───────────────────────────────────────────────────────────

def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _bar(value: float, max_val: float, width: int = 14) -> str:
    if max_val <= 0:
        return "░" * width
    filled = min(width, max(0, int(round(value / max_val * width))))
    return "█" * filled + "░" * (width - filled)


def _sw_line(val, label: str, c: dict) -> str:
    on = int(val or 0) == 1
    dot   = f"{c['G']}●{c['R']}" if on else f"{c['Gr']}○{c['R']}"
    state = "on " if on else "off"
    return f"  {dot} {state}  {label}"


def _extract_telemetry(status_resp: dict) -> dict:
    """Extract live telemetry dict from a device_status() response.

    Response shape: {"data": [{"status": {...temps...}, "is_online": 1, ...}]}
    The live values sit under data[0]["status"] (dict) or data[0]["telemetryData"].
    """
    if isinstance(status_resp, dict):
        data_list = status_resp.get("data") or status_resp.get("devices") or []
        item = data_list[0] if isinstance(data_list, list) and data_list else status_resp
    elif isinstance(status_resp, list) and status_resp:
        item = status_resp[0]
    else:
        item = {}
    _s = item.get("status")
    _td = item.get("telemetryData")
    td = _s if isinstance(_s, dict) else (_td if isinstance(_td, dict) else item)
    return td if isinstance(td, dict) else {}


def _extract_profile(profile_resp: dict) -> tuple[dict, dict]:
    """Return (status_data, profile) from a client.device() response.

    Handles {"data": {...}} or {"data": [{...}]} wrapping.
    """
    prof = profile_resp
    raw_data = prof.get("data") if isinstance(prof, dict) else None
    if isinstance(raw_data, dict):
        prof = raw_data
    elif isinstance(raw_data, list) and raw_data:
        prof = raw_data[0]
    sd = prof.get("status_data") or {}
    return sd, prof


def _format_status(status_resp: dict, profile_resp: dict, dev_name: str,
                   rules: list | None) -> str:
    td = _extract_telemetry(status_resp)
    sd, prof = _extract_profile(profile_resp)
    on = _use_color()
    c = {
        "R":  "\033[0m"  if on else "",
        "B":  "\033[1m"  if on else "",
        "G":  "\033[32m" if on else "",
        "Y":  "\033[33m" if on else "",
        "Re": "\033[31m" if on else "",
        "Gr": "\033[90m" if on else "",
    }

    rule_map: dict[str, str] = {}
    if rules:
        for r in (rules if isinstance(rules, list) else rules.get("data", [])):
            rule_map[r.get("id", "")] = r.get("name", r.get("id", ""))

    ts = datetime.datetime.now().strftime("%H:%M:%S")
    W = 58
    sep = "─" * W

    live_badge = f"{c['G']}● Live{c['R']}" if on else "● Live"
    lines: list[str] = [
        f"{c['B']}{dev_name:<42}{c['R']}  {live_badge} · {ts}",
        sep,
    ]

    # ── hot water — telemetry fields ──
    t1 = float(td.get("waterTemperature1") or 0)   # bottom (hottest)
    t2 = float(td.get("waterTemperature2") or 0)   # mid
    t3 = float(td.get("waterTemperature3") or 0)   # top (coolest)
    target = float(td.get("temperatureLimit") or 85)
    heater_on = int(td.get("heaterStatus") or 0) == 1
    heater_lbl = f"{c['Re']}ON{c['R']}" if heater_on else f"{c['Gr']}off{c['R']}"

    lines += [
        "",
        f"{c['B']}Hot Water{c['R']}   target {target:.0f} °C",
        f"  T3 Top   {t3:5.1f} °C  {_bar(t3, target)}",
        f"  T2 Mid   {t2:5.1f} °C  {_bar(t2, target)}",
        f"  T1 Bot   {t1:5.1f} °C  {_bar(t1, target)}",
        f"  Heater   {heater_lbl}",
    ]

    # ── boost ──
    boost_on = int(td.get("boostStatus") or 0) == 1
    # boost_power sits on the device profile, not in live telemetry
    boost_idx = int(prof.get("boost_power") or td.get("boost_power") or 4)
    pct = {1: "25%", 2: "50%", 3: "75%", 4: "100%"}.get(boost_idx, "?")
    if boost_on:
        boost_str = f"{c['Re']}ON{c['R']}   power {pct}"
    else:
        boost_str = f"{c['Gr']}OFF{c['R']}  power {pct}"
    lines += ["", f"{c['B']}Boost{c['R']}         {boost_str}"]

    # ── power flow ──
    grid_kw  = float(td.get("phasePower") or 0)
    solar_kw = float(td.get("solarStreamA") or 0)
    htrkw    = float(td.get("offPeakStreamHeaterPower") or 0)

    if grid_kw < 0:
        grid_dir = f"↓ {c['Y']}import{c['R']}"
        grid_abs = abs(grid_kw)
    else:
        grid_dir = f"↑ {c['G']}export{c['R']}"
        grid_abs = grid_kw

    lines += [
        "",
        f"{c['B']}Power Flow{c['R']}",
        f"  Grid     {grid_abs:5.2f} kW  {grid_dir}",
        f"  Solar    {solar_kw:5.2f} kW  ☀",
        f"  Heater   {htrkw:5.2f} kW",
    ]

    # ── switchers ──
    lines += [
        "",
        f"{c['B']}Switchers{c['R']}",
        _sw_line(sd.get("customer"),                       "PowerDiverter",     c),
        _sw_line(sd.get("offpeakSwitcherStatus"),          "Off-Peak",          c),
        _sw_line(sd.get("solarRestrictionSwitcherStatus"), "Solar Restriction", c),
        _sw_line(sd.get("priorityModeSwitcherStatus"),     "Priority Mode",     c),
        _sw_line(sd.get("weatherSwitcherStatus"),          "Weather Boost",     c),
    ]

    # ── active rule ──
    raw_rule = (
        sd.get("rule_customer")
        or (prof.get("device_statuses") or {}).get("customer_rule")
        or ""
    )
    if not raw_rule or raw_rule == "unknown":
        raw_rule = ""
    rule_display = rule_map.get(raw_rule, raw_rule or "—")
    lines += ["", f"{c['B']}Active Rule{c['R']}    {rule_display}"]

    # ── signal — telemetry has WiFi dBm; LoRa flag on profile ──
    wifi_dbm = td.get("signalWiFi")
    lora_on  = int(prof.get("is_lora_enabled") or td.get("is_lora_enabled") or 0) == 1
    wifi_str = f"WiFi {wifi_dbm} dBm" if wifi_dbm is not None else "WiFi —"
    lora_str = f"LoRa {'on' if lora_on else 'off'}"
    lines.append(f"{c['B']}Signal{c['R']}         {wifi_str} · {lora_str}")

    lines += ["", sep]
    return "\n".join(lines)


# ── Credentials / client ─────────────────────────────────────────────────────

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
    token = resp.get("access_token") or resp.get("token") or resp.get("accessToken")
    if not token:
        _out(resp)
        sys.exit("Login failed — check your email/password (or the account may use SSO).")
    return client


def _first_device(client: EnergipaysClient) -> tuple[str, str, str]:
    """Return (device_id, data_server, name) for the first device. Exits if none found."""
    resp = client.devices()
    devices = resp if isinstance(resp, list) else resp.get("data", [])
    if not isinstance(devices, list):
        devices = []
    if not devices:
        sys.exit("No devices found on this account.")
    dev = devices[0]
    return (
        dev["id"],
        dev.get("server", "https://data-au-1.energipays.com"),
        dev.get("name", "Device"),
    )


def _resolve_device(client: EnergipaysClient, args) -> tuple[str, str, str]:
    """Return (device_id, data_server, name).

    If --device is given, look it up in the device list to get the server URL and
    friendly name. Falls back to first device when --device is absent.
    """
    override = getattr(args, "device", None)
    if not override:
        return _first_device(client)
    # Fetch device list to resolve server + name for the given ID
    resp = client.devices()
    devices = resp if isinstance(resp, list) else resp.get("data", [])
    if not isinstance(devices, list):
        devices = []
    for d in devices:
        if d.get("id") == override:
            return (
                override,
                d.get("server", "https://data-au-1.energipays.com"),
                d.get("name", override),
            )
    # ID not in list — trust the override, use default server
    return override, "https://data-au-1.energipays.com", override


# ── Subcommand handlers ──────────────────────────────────────────────────────

_POWER_MAP   = {25: 1, 50: 2, 75: 3, 100: 4}
_POWER_LABEL = {1: "25%", 2: "50%", 3: "75%", 4: "100%"}
_PERIOD_LABEL = {1: "30 min", 2: "1 h", 3: "2 h"}


def _c() -> dict:
    on = _use_color()
    return {
        "R":  "\033[0m"  if on else "",
        "B":  "\033[1m"  if on else "",
        "G":  "\033[32m" if on else "",
        "Y":  "\033[33m" if on else "",
        "Re": "\033[31m" if on else "",
        "Gr": "\033[90m" if on else "",
        "Cy": "\033[36m" if on else "",
    }


def _format_devices(resp: dict | list) -> str:
    devs = resp if isinstance(resp, list) else resp.get("data", resp if isinstance(resp, list) else [])
    if not isinstance(devs, list):
        devs = [devs]
    co = _c()
    sep = "─" * 58
    n = len(devs)
    lines = [f"{co['B']}{n} device{'s' if n != 1 else ''} on this account{co['R']}", sep]
    for d in devs:
        online = int(d.get("is_online") or 0) == 1
        dot = f"{co['G']}● online{co['R']}" if online else f"{co['Gr']}○ offline{co['R']}"
        name = d.get("name") or d.get("id", "Unknown")
        dev_id = d.get("id", "")
        server = d.get("server", "")
        server_short = server.replace("https://", "").replace("http://", "")
        boost_idx = int(d.get("boost_power") or 0)
        boost_pwr = _POWER_LABEL.get(boost_idx, "")
        target = d.get("current_temperature") or d.get("temperatureLimit") or ""
        lines += [
            "",
            f"  {co['B']}{name}{co['R']}   {dot}",
            f"    ID:      {co['Gr']}{dev_id}{co['R']}",
            f"    Server:  {server_short}",
        ]
        if target:
            lines.append(f"    Target:  {target} °C")
        if boost_pwr:
            lines.append(f"    Boost power: {boost_pwr}")
    lines += ["", sep]
    return "\n".join(lines)


def _format_device(resp: dict) -> str:
    # Unwrap {"data": {...}} or {"data": [{...}]}
    prof = resp
    raw = prof.get("data") if isinstance(prof, dict) else None
    if isinstance(raw, dict):
        prof = raw
    elif isinstance(raw, list) and raw:
        prof = raw[0]
    co = _c()
    sep = "─" * 58
    name = prof.get("name") or prof.get("id", "Device")
    dev_id = prof.get("id", "")
    online = int(prof.get("is_online") or 0) == 1
    dot = f"{co['G']}● online{co['R']}" if online else f"{co['Gr']}○ offline{co['R']}"
    boost_idx = int(prof.get("boost_power") or 0)
    boost_pwr = _POWER_LABEL.get(boost_idx, "—")
    lora = int(prof.get("is_lora_enabled") or 0) == 1
    target = prof.get("current_temperature") or prof.get("temperatureLimit") or "—"
    firmware = prof.get("firmware_version") or "—"
    volume = prof.get("volume") or "—"
    phase = prof.get("phase_type") or "—"
    sn = prof.get("ws_serial_number") or "—"
    sd = prof.get("status_data") or {}
    lines = [
        f"{co['B']}{name}{co['R']}   {dot}",
        sep,
        f"  ID:           {co['Gr']}{dev_id}{co['R']}",
        f"  Target temp:  {target} °C",
        f"  Boost power:  {boost_pwr}",
        f"  Volume:       {volume}",
        f"  Firmware:     {firmware}",
        f"  Phase:        {phase}",
        f"  Serial:       {sn}",
        f"  LoRa:         {'on' if lora else 'off'}",
    ]
    # Switcher states if available
    sw_fields = [
        ("customer",                       "PowerDiverter"),
        ("offpeakSwitcherStatus",          "Off-Peak"),
        ("solarRestrictionSwitcherStatus", "Solar Restriction"),
        ("priorityModeSwitcherStatus",     "Priority Mode"),
        ("weatherSwitcherStatus",          "Weather Boost"),
    ]
    sw_lines = []
    for field, label in sw_fields:
        val = sd.get(field)
        if val is not None:
            on_val = int(val) == 1
            dot_sw = f"{co['G']}●{co['R']}" if on_val else f"{co['Gr']}○{co['R']}"
            sw_lines.append(f"    {dot_sw} {'on ' if on_val else 'off'}  {label}")
    if sw_lines:
        lines += ["", f"  {co['B']}Switchers{co['R']}"] + sw_lines
    lines += ["", sep]
    return "\n".join(lines)


def cmd_me(c, a):            return c.me()
def cmd_rules(c, a):         return c.rules()
def cmd_logs(c, a):          return c.logs({"limit": a.limit} if a.limit else {})

def cmd_devices(c, a):
    resp = c.devices()
    if getattr(a, "json", False) or not sys.stdout.isatty():
        return resp
    return _format_devices(resp)

def cmd_device(c, a):
    dev_id = getattr(a, "device_id", None) or _resolve_device(c, a)[0]
    resp = c.device(dev_id)
    if getattr(a, "json", False) or not sys.stdout.isatty():
        return resp
    return _format_device(resp)

def cmd_status(c, a):
    dev_id, data_server, dev_name = _resolve_device(c, a)
    # legacy positional override (kept for scripts)
    if getattr(a, "device_ids", None):
        dev_id = a.device_ids[0]

    status_resp = c.device_status([dev_id])

    # --json or piped output: raw dict
    if getattr(a, "json", False) or not sys.stdout.isatty():
        return status_resp

    # Human-friendly: also fetch device profile (for switcher states + boost_power)
    # and rules (for active rule name). Both are best-effort — failures show "—".
    profile_resp: dict = {}
    rules = None
    try:
        profile_resp = c.device(dev_id)
    except Exception:
        pass
    try:
        rules = c.rules()
        if isinstance(rules, dict):
            rules = rules.get("data", [])
    except Exception:
        pass

    return _format_status(status_resp, profile_resp, dev_name, rules)


def cmd_statistics(c, a):
    dev_id, server, _ = _resolve_device(c, a)
    return c.statistics(dev_id, getattr(a, "server", None) or server, tz=a.tz, phase=a.phase)

def cmd_stats(c, a):
    dev_id, server, _ = _resolve_device(c, a)
    return c.stats(dev_id, a.date_from, a.date_to,
                   getattr(a, "server", None) or server,
                   tz=a.tz, data_type=a.data_type, phase=a.phase)

def cmd_boost(c, a):
    if not a.yes:
        sys.exit("Refusing to send a boost command without --yes (this controls a device).")
    dev_id, server, _ = _resolve_device(c, a)
    server = getattr(a, "server", None) or server

    # Fail fast if boost already active (mirrors bridge CLI behaviour)
    try:
        td = _extract_telemetry(c.device_status([dev_id]))
        if int(td.get("boostStatus") or 0):
            sys.exit(
                "Boost is already active — use 'cancel --yes' first.\n"
                f"  T1={td.get('waterTemperature1','—')}  "
                f"T2={td.get('waterTemperature2','—')}  "
                f"T3={td.get('waterTemperature3','—')} °C"
            )
    except SystemExit:
        raise
    except Exception:
        pass  # status check failure is non-fatal; proceed with boost

    # Optionally set power first
    if getattr(a, "power", None) is not None:
        idx = _POWER_MAP[a.power]
        c.set_boost_power(dev_id, idx)

    return c.boost_device(dev_id, server, period=a.period)

def cmd_device_on(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    return c.set_device_status(_resolve_device(c, a)[0], status=1)

def cmd_device_off(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    return c.set_device_status(_resolve_device(c, a)[0], status=0)

def cmd_heater_on(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    return c.set_device_status(_resolve_device(c, a)[0], heaterStatus=1)

def cmd_heater_off(c, a):
    if not a.yes:
        sys.exit("Refusing to send a device command without --yes.")
    return c.set_device_status(_resolve_device(c, a)[0], heaterStatus=0)

def cmd_cancel(c, a):
    if not a.yes:
        sys.exit("Refusing to send a cancel command without --yes.")
    dev_id, server, _ = _resolve_device(c, a)
    return c.cancel_boost(dev_id, getattr(a, "server", None) or server)

def cmd_power(c, a):
    if not a.yes:
        sys.exit("Refusing to send a power command without --yes.")
    if a.percent not in (25, 50, 75, 100):
        sys.exit("--percent must be 25, 50, 75, or 100.")
    return c.set_boost_power(_resolve_device(c, a)[0], _POWER_MAP[a.percent])

def cmd_key(c, a):
    import energipays as ep
    import base64
    c._ensure_key()
    return {"key_b64": base64.b64encode(ep._KEY).decode() if ep._KEY else None}


# ── Parser ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="energipays",
        description="Standalone command-line client for the Energipays API.",
        epilog="Credentials: --email/--password, else ENERGIPAYS_EMAIL/ENERGIPAYS_PASSWORD, else prompt.",
    )
    p.add_argument("-e", "--email", help="account email (or set ENERGIPAYS_EMAIL)")
    p.add_argument("-p", "--password",
                   help="account password (insecure; prefer env or --password-stdin)")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin")
    p.add_argument("--base-url", default="https://energipays.com", help="API base URL")
    p.add_argument("--json", action="store_true",
                   help="output raw JSON (status/devices/device default to human-friendly)")
    p.add_argument("--device", metavar="DEVICE_ID",
                   help="target a specific device by ID (default: first device on account)")

    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name, handler, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=handler)
        return sp

    add("me",      cmd_me,      "show the authenticated user profile")
    add("devices", cmd_devices, "list all devices (human-friendly; --json for raw)")
    add("rules",   cmd_rules,   "list automation rules")
    add("key",     cmd_key,     "resolve and print the AES key (base64)")

    sp = add("device", cmd_device,
             "show a single device detail (defaults to first device; --json for raw)")
    sp.add_argument("device_id", nargs="?", default=None)

    sp = add("logs", cmd_logs, "fetch energy/telemetry logs")
    sp.add_argument("--limit", type=int, default=5)

    sp = add("status", cmd_status,
             "live device status — human-friendly by default, --json for raw")
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

    sp = add("cancel", cmd_cancel, "cancel a running boost")
    sp.add_argument("--yes", action="store_true",
                    help="confirm sending the device command")

    sp = add("power", cmd_power, "set boost power level without triggering a boost")
    sp.add_argument("--percent", type=int, default=100, choices=[25, 50, 75, 100],
                    help="boost power percentage (default: 100)")
    sp.add_argument("--yes", action="store_true",
                    help="confirm sending the device command")

    sp = add("boost", cmd_boost,
             "trigger a timed boost — checks if already active first")
    sp.add_argument("--period", type=int, default=2, choices=[1, 2, 3],
                    help="boost duration: 1=30min, 2=1h (default), 3=2h")
    sp.add_argument("--power", type=int, choices=[25, 50, 75, 100], default=None,
                    metavar="{25,50,75,100}",
                    help="set boost power %% before starting (omit to keep current setting)")
    sp.add_argument("--yes", action="store_true",
                    help="confirm sending the device command")

    for name, handler, help_text in [
        ("device-on",  cmd_device_on,  "enable the PowerDiverter"),
        ("device-off", cmd_device_off, "disable the PowerDiverter"),
        ("heater-on",  cmd_heater_on,  "turn the immersion heater on"),
        ("heater-off", cmd_heater_off, "turn the immersion heater off"),
    ]:
        sp = add(name, handler, help_text)
        sp.add_argument("--yes", action="store_true",
                        help="confirm sending the device command")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = _make_client(args)
    result = args.func(client, args)
    if isinstance(result, str):
        print(result)
    else:
        _out(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
