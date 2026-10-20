r"""Shared fixtures. pytest discovers this file automatically.

Fixture graph:

    broker (session)  ->  probe (per test)  ->  device_client (per test)
                     \->  device (per test)

Each test gets a fresh device with a unique id, so retained messages and
leftover state from one test can never leak into another.
"""

import json
import os
import uuid
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from simulator import ThermostatSimulator
from tests.helpers.broker import EmbeddedBroker
from tests.helpers.probe import DeviceClient, MqttProbe

SCHEMA_DIR = Path(__file__).parent / "schemas"

# Telemetry interval used by most tests. Short keeps the suite fast.
FAST_INTERVAL_S = 0.1


@pytest.fixture(scope="session")
def broker():
    """Yields (host, port). Uses MQTT_HOST/MQTT_PORT if set, else starts an embedded broker."""
    if os.environ.get("MQTT_HOST"):
        yield os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", 1883))
        return
    embedded = EmbeddedBroker()
    embedded.start()
    yield embedded.host, embedded.port
    embedded.stop()


@pytest.fixture
def device_id():
    return f"thermo-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def probe(broker, device_id):
    """A connected test client already listening to everything the device publishes."""
    host, port = broker
    p = MqttProbe(host, port).connect()
    p.subscribe(f"devices/{device_id}/#")
    yield p
    p.disconnect()


@pytest.fixture
def make_device(broker, device_id):
    """Factory fixture: lets a test choose constructor args, still guarantees cleanup."""
    host, port = broker
    created = []

    def _make(**kwargs):
        kwargs.setdefault("interval_s", FAST_INTERVAL_S)
        kwargs.setdefault("seed", 42)
        dev = ThermostatSimulator(device_id, host, port, **kwargs)
        created.append(dev)
        return dev

    yield _make
    for dev in created:
        dev.stop()


@pytest.fixture
def device(make_device, probe):
    """A running device. Waits until it reports online so tests start from a known state."""
    dev = make_device()
    dev.start()
    probe.wait_for(f"devices/{dev.device_id}/status", lambda m: m.json()["state"] == "online")
    return dev


@pytest.fixture
def device_client(probe, device):
    return DeviceClient(probe, device.device_id)


def _load_validator(name):
    return Draft202012Validator(json.loads((SCHEMA_DIR / name).read_text()))


@pytest.fixture(scope="session")
def telemetry_validator():
    return _load_validator("telemetry.schema.json")


@pytest.fixture(scope="session")
def response_validator():
    return _load_validator("response.schema.json")
