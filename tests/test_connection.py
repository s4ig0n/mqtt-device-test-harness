"""Connection lifecycle: online announcement, retained status, Last Will."""

import re

import pytest

from tests.helpers.probe import MqttProbe


def status_is(state):
    return lambda m: m.json()["state"] == state


@pytest.mark.smoke
def test_device_announces_online_with_firmware_version(device, probe):
    msg = probe.wait_for(f"devices/{device.device_id}/status", status_is("online"))

    assert re.fullmatch(r"\d+\.\d+\.\d+", msg.json()["fw"]), "firmware should be semver"


def test_status_is_retained_for_late_subscribers(device, broker):
    # A dashboard that connects AFTER the device came online must still learn its state.
    host, port = broker
    late = MqttProbe(host, port).connect()
    try:
        late.subscribe(f"devices/{device.device_id}/status")
        msg = late.wait_for(f"devices/{device.device_id}/status")

        assert msg.retain is True
        assert msg.json()["state"] == "online"
    finally:
        late.disconnect()


def test_graceful_stop_publishes_offline(device, probe):
    device.stop()

    probe.wait_for(f"devices/{device.device_id}/status", status_is("offline"))


@pytest.mark.smoke
def test_crash_triggers_last_will(device, probe):
    bookmark = probe.bookmark()

    device.crash()

    # The device never sent "offline" itself. Only the broker's LWT can produce this.
    probe.wait_for(f"devices/{device.device_id}/status", status_is("offline"), start=bookmark)


def test_last_will_is_retained(device, probe, broker):
    bookmark = probe.bookmark()
    device.crash()
    probe.wait_for(f"devices/{device.device_id}/status", status_is("offline"), start=bookmark)

    host, port = broker
    late = MqttProbe(host, port).connect()
    try:
        late.subscribe(f"devices/{device.device_id}/status")
        msg = late.wait_for(f"devices/{device.device_id}/status")
        assert msg.json()["state"] == "offline"
    finally:
        late.disconnect()


def test_no_telemetry_after_stop(device, probe):
    topic = f"devices/{device.device_id}/telemetry"
    probe.wait_for(topic)
    device.stop()
    probe.wait_for(f"devices/{device.device_id}/status", status_is("offline"))

    # Negative test: assert something does NOT happen during a window.
    probe.assert_no_message(topic, within=0.5)
