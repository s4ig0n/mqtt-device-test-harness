"""Fault injection and recovery: what the device does when things go wrong."""

import pytest


def telemetry(device):
    return f"devices/{device.device_id}/telemetry"


def status(device):
    return f"devices/{device.device_id}/status"


def test_sensor_fault_reports_null_temperature(device_client, probe, device, telemetry_validator):
    device_client.send("inject_fault", {"type": "sensor_disconnected"})

    msg = probe.wait_for(telemetry(device), lambda m: m.json()["fault"] == "sensor_disconnected")

    telemetry_validator.validate(msg.json())  # null temperature is still a valid payload
    assert msg.json()["temperature_c"] is None


def test_heater_fails_safe_when_sensor_is_lost(device_client, probe, device):
    """Safety requirement: never run the heater without a temperature reading."""
    device_client.send("set_setpoint", {"celsius": 35})
    probe.wait_for(telemetry(device), lambda m: m.json()["heater_on"] is True)

    device_client.send("inject_fault", {"type": "sensor_disconnected"})
    start = probe.bookmark()

    for msg in probe.wait_for_count(telemetry(device), 5, start=start):
        assert msg.json()["heater_on"] is False


def test_clearing_fault_restores_readings(device_client, probe, device):
    device_client.send("inject_fault", {"type": "sensor_disconnected"})
    probe.wait_for(telemetry(device), lambda m: m.json()["temperature_c"] is None)

    device_client.send("clear_fault")

    msg = probe.wait_for(telemetry(device), lambda m: m.json()["fault"] is None, start=probe.bookmark())
    assert isinstance(msg.json()["temperature_c"], float)


@pytest.mark.slow
def test_reboot_goes_offline_then_online(device_client, probe, device):
    start = probe.bookmark()

    assert device_client.send("reboot")["ok"] is True

    offline = probe.wait_for(status(device), lambda m: m.json()["state"] == "offline", start=start)
    probe.wait_for(
        status(device),
        lambda m: m.json()["state"] == "online" and m.received_at > offline.received_at,
        start=start,
    )


@pytest.mark.slow
def test_reboot_resets_sequence_and_clears_fault(device_client, probe, device):
    probe.wait_for_count(telemetry(device), 5)
    device_client.send("inject_fault", {"type": "sensor_disconnected"})
    start = probe.bookmark()

    device_client.send("reboot")
    probe.wait_for(status(device), lambda m: m.json()["state"] == "online", start=start)

    first_after_boot = probe.wait_for(telemetry(device), lambda m: m.json()["seq"] == 0, start=start)
    assert first_after_boot.json()["fault"] is None
    assert device_client.send("ping")["ok"] is True
