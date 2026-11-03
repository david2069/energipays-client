"""Rule activation (#2): the body energipays.com sends, never the short form."""
import copy

import pytest

from conftest import DEVICE_ID, Resp, sent_body


def _route_device(c, device):
    c.session.add("GET", f"/api/devices/{DEVICE_ID}", Resp(200, device, encrypted=True))


@pytest.mark.parametrize("circuit, field", [
    ("command", "rule_customer"), ("offpeak", "rule_offpeak"), ("heater2", "rule_heater2"),
])
def test_activate_sends_current_status_data_with_only_the_circuit_field_changed(
        client, device_fixture, circuit, field):
    _route_device(client, device_fixture)
    client.session.add("PUT", f"/api/devices/{DEVICE_ID}", Resp(200, {"ok": 1}, encrypted=True))

    assert client.activate_rule(DEVICE_ID, "rule-1", circuit) == {"ok": 1}

    put = [c for c in client.session.calls if c[0] == "PUT"]
    assert len(put) == 1
    body = sent_body(put[0])
    expected = copy.deepcopy(device_fixture["data"]["status_data"])
    expected[field] = "rule-1"
    assert body == {"status_data": expected, "rule_id": "rule-1", "type": circuit}
    # admin / customer / retailer are carried over, never forced to 1
    assert body["status_data"]["customer"] == 0


def test_clear_sends_zero(client, device_fixture):
    _route_device(client, device_fixture)
    client.session.add("PUT", f"/api/devices/{DEVICE_ID}", Resp(200, {}, encrypted=True))
    client.activate_rule(DEVICE_ID, "0")
    body = sent_body([c for c in client.session.calls if c[0] == "PUT"][0])
    assert body["rule_id"] == "0" and body["status_data"]["rule_customer"] == "0"


def test_set_rule_no_longer_sends_the_short_body(client, device_fixture):
    _route_device(client, device_fixture)
    client.session.add("PUT", f"/api/devices/{DEVICE_ID}", Resp(200, {}, encrypted=True))
    client.set_rule(DEVICE_ID, "rule-1", "offpeak")
    body = sent_body([c for c in client.session.calls if c[0] == "PUT"][0])
    assert "status_data" in body and body["type"] == "offpeak"


@pytest.mark.parametrize("group", ["g-1", 7])
def test_device_in_a_group_is_skipped_without_a_write(client, device_fixture, group):
    device_fixture["data"]["group_id"] = group
    _route_device(client, device_fixture)
    res = client.activate_rule(DEVICE_ID, "rule-1")
    assert "skipped" in res
    assert not [c for c in client.session.calls if c[0] == "PUT"]


@pytest.mark.parametrize("group", ["0", "empty", None, "", "unknown"])
def test_no_group_values_still_activate(client, device_fixture, group):
    device_fixture["data"]["group_id"] = group
    _route_device(client, device_fixture)
    client.session.add("PUT", f"/api/devices/{DEVICE_ID}", Resp(200, {}, encrypted=True))
    assert "skipped" not in client.activate_rule(DEVICE_ID, "rule-1")


def test_device_read_failure_returns_the_error_and_writes_nothing(client):
    client.session.add("GET", f"/api/devices/{DEVICE_ID}", Resp(500, {"message": "Server Error"}))
    res = client.activate_rule(DEVICE_ID, "rule-1")
    assert res["status"] == 500
    assert not [c for c in client.session.calls if c[0] == "PUT"]


def test_missing_status_data_writes_nothing(client):
    client.session.add("GET", f"/api/devices/{DEVICE_ID}", Resp(200, {"data": {"id": DEVICE_ID}}, encrypted=True))
    assert "error" in client.activate_rule(DEVICE_ID, "rule-1")
    assert not [c for c in client.session.calls if c[0] == "PUT"]


def test_put_500_is_returned_as_an_error(client, device_fixture):
    _route_device(client, device_fixture)
    client.session.add("PUT", f"/api/devices/{DEVICE_ID}", Resp(500, {"message": "Server Error"}))
    assert client.activate_rule(DEVICE_ID, "rule-1")["status"] == 500


def test_unknown_circuit_is_rejected(client):
    with pytest.raises(ValueError):
        client.activate_rule(DEVICE_ID, "rule-1", "off_peak")


def test_update_device_takes_the_circuit(client):
    client.session.add("PUT", f"/api/devices/{DEVICE_ID}", Resp(200, {}, encrypted=True))
    client.update_device(DEVICE_ID, {"rule_heater2": "r"}, "r", rule_type="heater2")
    assert sent_body(client.session.calls[0])["type"] == "heater2"
