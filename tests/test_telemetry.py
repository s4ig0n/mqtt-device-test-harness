"""Telemetry stream: payload contract, ordering, timing, and device behaviour."""

import statistics

import pytest


def telemetry_topic(device):
    return f"devices/{device.device_id}/telemetry"


@pytest.mark.smoke
def test_telemetry_matches_schema(device, probe, telemetry_validator):
    messages = probe.wait_for_count(telemetry_topic(device), 5)

    for msg in messages:
        errors = list(telemetry_validator.iter_errors(msg.json()))
        assert not errors, f"seq {msg.json().get('seq')}: {[e.message for e in errors]}"


def test_payload_device_id_matches_topic(device, probe):
    msg = probe.wait_for(telemetry_topic(device))

    assert msg.json()["device_id"] == device.device_id


def test_sequence_numbers_start_at_zero_and_have_no_gaps(device, probe):
    seqs = [m.json()["seq"] for m in probe.wait_for_count(telemetry_topic(device), 10)]

    assert seqs == list(range(10))


def test_timestamps_increase(device, probe):
    stamps = [m.json()["ts"] for m in probe.wait_for_count(telemetry_topic(device), 5)]

    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps), "duplicate timestamps"


@pytest.mark.slow
@pytest.mark.parametrize("interval_s", [0.1, 0.3])
def test_publish_rate_matches_configured_interval(device_client, probe, device, interval_s):
    device_client.send("set_interval", {"seconds": interval_s})
    probe.clear()

    messages = probe.wait_for_count(telemetry_topic(device), 6, timeout=interval_s * 6 + 5)
    gaps = [b.received_at - a.received_at for a, b in zip(messages, messages[1:])]

    # Timing on a shared CI box is noisy: assert on the median with a tolerance.
    assert statistics.median(gaps) == pytest.approx(interval_s, rel=0.35)


def test_heater_turns_on_and_room_warms_when_setpoint_is_high(device_client, probe, device):
    device_client.send("set_setpoint", {"celsius": 30})
    start = probe.bookmark()

    readings = [m.json() for m in probe.wait_for_count(telemetry_topic(device), 5, start=start)]

    assert all(r["heater_on"] for r in readings)
    temps = [r["temperature_c"] for r in readings]
    assert temps[-1] > temps[0], f"temperature should rise while heating: {temps}"


def test_heater_stays_off_when_setpoint_is_below_room(device_client, probe, device):
    device_client.send("set_setpoint", {"celsius": 10})
    start = probe.bookmark()

    readings = [m.json() for m in probe.wait_for_count(telemetry_topic(device), 5, start=start)]

    assert all(r["heater_on"] is False for r in readings)
    assert all(r["setpoint_c"] == 10 for r in readings)
