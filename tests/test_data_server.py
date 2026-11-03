"""Data-server calls (#3): token refresh, one retry on 401, 202 = queued; boost periods (#4)."""
import threading
import time
import urllib.parse

import pytest

import energipays
from conftest import DATA, DEVICE_ID, Resp, fake_jwt


def _expire(c):
    c._set_access_token(fake_jwt(time.time() - 10))


def _fake_refresh(c, counter=None):
    def refresh():
        if counter is not None:
            counter.append(1)
        c._set_access_token(fake_jwt(time.time() + 3600))
        return {}
    c.refresh = refresh


def test_expired_token_is_refreshed_before_a_data_call(client):
    _expire(client)
    n = []
    _fake_refresh(client, n)
    client.session.add("POST", "/statistics", Resp(200, {"today": {}}))
    assert client.statistics(DEVICE_ID, DATA) == {"today": {}}
    assert n == [1]


def test_401_refreshes_and_retries_once(client):
    n = []
    _fake_refresh(client, n)
    client.session.add("POST", "/status", Resp(401, {"message": "Unauthenticated."}))
    client.session.add("POST", "/status", Resp(200, {"data": []}))
    assert client.device_status([DEVICE_ID]) == {"data": []}
    assert n == [1] and len(client.session.calls) == 2


def test_second_401_is_returned_not_retried_again(client):
    _fake_refresh(client)
    client.session.add("POST", "/boost", Resp(401, {}))
    client.session.add("POST", "/boost", Resp(401, {}))
    assert client.boost_device(DEVICE_ID, DATA, period=2)["status"] == 401
    assert len(client.session.calls) == 2


@pytest.mark.parametrize("call", [
    lambda c: c.stats(DEVICE_ID, "2026-01-01", "2026-01-02", DATA),
    lambda c: c.cancel_boost(DEVICE_ID, DATA),
    lambda c: c.cancel_weather_boost(DEVICE_ID, DATA),
])
def test_every_data_call_retries_on_401(client, call):
    _fake_refresh(client)
    client.session.add("POST", DATA, Resp(401, {}))
    client.session.add("POST", DATA, Resp(200, {"ok": 1}))
    assert call(client) == {"ok": 1}


def test_202_is_reported_as_queued(client):
    client.session.add("POST", "/boost", Resp(202, {"message": "queued"}))
    res = client.boost_device(DEVICE_ID, DATA, period=2)
    assert res["queued"] is True and res["status"] == 202 and "error" not in res


def test_500_is_an_error(client):
    client.session.add("POST", "/boost", Resp(500, {}))
    assert client.boost_device(DEVICE_ID, DATA, period=2)["status"] == 500


def _period(call):
    return urllib.parse.parse_qs(call[2]["data"])["period"][0]


@pytest.mark.parametrize("hours, period", [(1, "2"), (2, "3"), (3, "4")])
def test_boost_periods_match_the_site(client, hours, period):
    client.session.add("POST", "/boost", Resp(200, {}))
    client.boost_device(DEVICE_ID, DATA, period=energipays.BOOST_PERIODS[hours])
    assert _period(client.session.calls[0]) == period


def test_default_boost_is_one_hour_not_cancel(client):
    client.session.add("POST", "/boost", Resp(200, {}))
    client.boost_device(DEVICE_ID, DATA)
    assert _period(client.session.calls[0]) == "2"


def test_cancel_sends_period_1(client):
    client.session.add("POST", "/boost", Resp(200, {}))
    client.cancel_boost(DEVICE_ID, DATA)
    assert _period(client.session.calls[0]) == "1"


@pytest.mark.parametrize("bad", [0, 5, "2"])
def test_invalid_period_is_refused_without_a_request(client, bad):
    with pytest.raises(ValueError):
        client.boost_device(DEVICE_ID, DATA, period=bad)
    assert client.session.calls == []


def test_concurrent_refreshes_are_serialised(client):
    _expire(client)
    active, peak = [0], [0]

    def refresh():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return {}
    client.refresh = refresh
    client._email = client._password = ""  # no login fallback
    threads = [threading.Thread(target=client._ensure_token) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
