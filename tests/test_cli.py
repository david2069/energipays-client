"""CLI (#4): positional device id is honoured; cached token avoids a password login."""
import time
import types

import cli
from conftest import fake_jwt


class Dev:
    def devices(self):
        return {"data": [{"id": "dev-b", "server": "https://data-x", "name": "B"},
                         {"id": "dev-a", "server": "https://data-y", "name": "A"}]}


def test_positional_device_id_is_used():
    args = types.SimpleNamespace(device=None, device_id="dev-a")
    assert cli._resolve_device(Dev(), args) == ("dev-a", "https://data-y", "A")


def test_flag_still_wins():
    args = types.SimpleNamespace(device="dev-b", device_id="dev-a")
    assert cli._resolve_device(Dev(), args)[0] == "dev-b"


def test_no_device_uses_first():
    args = types.SimpleNamespace(device=None, device_id=None)
    assert cli._resolve_device(Dev(), args)[0] == "dev-b"


def test_valid_cached_token_skips_login(monkeypatch, tmp_path):
    import energipays
    monkeypatch.setattr(energipays, "_TOKEN_CACHE", tmp_path / "t.json")
    energipays._TOKEN_CACHE.write_text(
        '{"access_token": "%s", "refresh_token": "r"}' % fake_jwt(time.time() + 3600))
    monkeypatch.setattr(cli, "EnergipaysClient", energipays.EnergipaysClient)

    def boom(self, *a, **k):
        raise AssertionError("login() called despite a valid cached token")
    monkeypatch.setattr(energipays.EnergipaysClient, "login", boom)
    args = types.SimpleNamespace(email="user@example.com", password="x", password_stdin=False,
                                 base_url=energipays.BASE_URL)
    assert cli._make_client(args)._token
