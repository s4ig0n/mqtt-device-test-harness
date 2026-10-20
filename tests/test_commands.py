"""Command / response channel: happy paths, boundaries, and bad input."""

import uuid

import pytest


@pytest.mark.smoke
def test_ping(device_client, response_validator):
    response = device_client.send("ping")

    response_validator.validate(response)
    assert response["ok"] is True
    assert response["result"] == {"pong": True}


def test_get_state_reports_defaults(device_client):
    state = device_client.send("get_state")["result"]

    assert state["setpoint_c"] == 21.0
    assert state["interval_s"] == pytest.approx(0.1)
    assert state["fault"] is None


# Boundary value analysis: test exactly at the edges of the valid range.
@pytest.mark.parametrize(
    "action, params, field, expected",
    [
        ("set_interval", {"seconds": 0.05}, "interval_s", 0.05),
        ("set_interval", {"seconds": 60}, "interval_s", 60.0),
        ("set_setpoint", {"celsius": 5}, "setpoint_c", 5.0),
        ("set_setpoint", {"celsius": 35}, "setpoint_c", 35.0),
        ("set_setpoint", {"celsius": 22.5}, "setpoint_c", 22.5),
    ],
    ids=["interval-min", "interval-max", "setpoint-min", "setpoint-max", "setpoint-float"],
)
def test_valid_settings_are_accepted_and_persisted(device_client, response_validator, action, params, field, expected):
    response = device_client.send(action, params)

    response_validator.validate(response)
    assert response["ok"] is True
    assert device_client.send("get_state")["result"][field] == expected


# Each row is one way a client can get it wrong. The device must say *why*.
@pytest.mark.parametrize(
    "action, params, expected_error",
    [
        ("set_interval", {"seconds": 0.049}, "out_of_range:seconds"),
        ("set_interval", {"seconds": 60.1}, "out_of_range:seconds"),
        ("set_interval", {"seconds": -1}, "out_of_range:seconds"),
        ("set_interval", {"seconds": "fast"}, "invalid_param:seconds"),
        ("set_interval", {"seconds": True}, "invalid_param:seconds"),
        ("set_interval", {}, "invalid_param:seconds"),
        ("set_setpoint", {"celsius": 4.9}, "out_of_range:celsius"),
        ("set_setpoint", {"celsius": 35.1}, "out_of_range:celsius"),
        ("set_setpoint", {"celsius": None}, "invalid_param:celsius"),
        ("inject_fault", {"type": "meltdown"}, "invalid_param:type"),
        ("self_destruct", {}, "unknown_action:self_destruct"),
    ],
)
def test_invalid_commands_are_rejected_with_reason(device_client, response_validator, action, params, expected_error):
    response = device_client.send(action, params)

    response_validator.validate(response)
    assert response["ok"] is False
    assert response["error"] == expected_error


def test_rejected_command_does_not_change_state(device_client):
    before = device_client.send("get_state")["result"]

    device_client.send("set_setpoint", {"celsius": 99})

    assert device_client.send("get_state")["result"] == before


@pytest.mark.parametrize("payload", [b"{not json", b"", b"\xff\xfe", b"[1, 2, 3]", b'"just a string"'])
def test_malformed_payload_gets_error_response(device, probe, response_validator, payload):
    start = probe.bookmark()

    probe.publish(f"devices/{device.device_id}/cmd", payload)

    msg = probe.wait_for(f"devices/{device.device_id}/cmd/response", start=start)
    response_validator.validate(msg.json())
    assert msg.json()["error"] == "malformed_json"


def test_device_still_works_after_garbage_input(device, probe, device_client):
    probe.publish(f"devices/{device.device_id}/cmd", b"\x00\x01garbage")

    assert device_client.send("ping")["ok"] is True


def test_responses_are_correlated_by_request_id(device, probe):
    """Fire several commands without waiting, then check every one got its own answer."""
    ids = [uuid.uuid4().hex for _ in range(10)]
    for request_id in ids:
        probe.publish(f"devices/{device.device_id}/cmd", {"id": request_id, "action": "ping"})

    responses = probe.wait_for_count(f"devices/{device.device_id}/cmd/response", len(ids))

    assert sorted(m.json()["id"] for m in responses) == sorted(ids)


def test_setpoint_change_shows_up_in_telemetry(device_client, probe, device):
    device_client.send("set_setpoint", {"celsius": 26})

    probe.wait_for(f"devices/{device.device_id}/telemetry", lambda m: m.json()["setpoint_c"] == 26)
