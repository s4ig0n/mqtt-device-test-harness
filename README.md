# MQTT Device Test Harness

[![tests](https://github.com/s4ig0n/mqtt-device-test-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/s4ig0n/mqtt-device-test-harness/actions/workflows/tests.yml)

A pytest-based test automation harness for an IoT device that communicates over MQTT.

The device under test is a simulated smart thermostat (`simulator/`). The harness (`tests/`) drives it
the way a real QA/SDET team would test connected hardware: through its public messaging interface only,
with no reaching into its internals.

## What it tests

| Area | Examples |
|---|---|
| Connection lifecycle | online announcement, retained status for late subscribers, Last Will on crash, graceful shutdown |
| Telemetry contract | JSON Schema validation, contiguous sequence numbers, monotonic timestamps, publish rate vs. configured interval |
| Command / response | happy paths, boundary values, invalid params, unknown actions, malformed payloads, request-id correlation |
| Fault handling | injected sensor failure, fail-safe heater shutdown, recovery, reboot cycle |
| Broker conformance | retained message delivery rules (see "Bug found" below) |

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pytest
```

No external broker is needed: the suite starts an in-process MQTT broker (amqtt) automatically.

To run against a real broker instead (e.g. Mosquitto):

```bash
docker run -d -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
MQTT_HOST=127.0.0.1 MQTT_PORT=1883 pytest
```

Useful selections:

```bash
pytest -m smoke          # fast critical-path checks
pytest -m "not slow"     # skip timing-based tests
pytest -k commands -v    # just the command tests, verbose
```

## Project layout

```
simulator/
  device.py              the device under test (MQTT thermostat)
  __main__.py            run it by hand: python -m simulator --port 1883
tests/
  conftest.py            fixtures: broker, probe, device, schema validators
  helpers/broker.py      embedded MQTT broker on a background thread
  helpers/probe.py       test client: subscribe, record, wait-for-with-timeout
  schemas/               JSON Schemas for telemetry and command responses
  test_*.py              the test suites
```

## Device contract

Base topic: `devices/<device_id>`

| Topic | Direction | Notes |
|---|---|---|
| `status` | device -> | retained, `{"state": "online"\|"offline", "fw": "x.y.z"}`, offline also set as Last Will |
| `telemetry` | device -> | QoS 0, every `interval_s` |
| `cmd` | -> device | QoS 1, `{"id": "...", "action": "...", "params": {...}}` |
| `cmd/response` | device -> | QoS 1, `{"id": "...", "ok": bool, "result": {...}, "error": str\|null}` |

Actions: `ping`, `get_state`, `set_interval`, `set_setpoint`, `inject_fault`, `clear_fault`, `reboot`.

## Design decisions

- **No `sleep()` in tests.** Async systems are tested by waiting for a condition with a timeout
  (`probe.wait_for(...)`). This makes tests fast when things work and gives a clear message when they don't.
- **Isolation per test.** Every test gets a fresh device with a unique ID, so retained messages and state
  never leak between tests.
- **Contract-first assertions.** Payloads are validated against JSON Schemas, so a field rename or type
  change fails loudly.
- **Hang protection.** `pytest-timeout` fails any test that runs longer than 30 seconds instead of freezing CI.

## Bug found: broker leaks retained messages

While building the suite, newly started devices began answering commands nobody had sent. The tests traced
this to the embedded broker, **amqtt 0.12.1**: when a client connects, it replays retained messages that
match *other clients'* subscriptions, not just the connecting client's own. MQTT 3.1.1 only allows retained
messages to be delivered for the subscriber's own matching subscriptions.

Response:
1. The device now ignores any message that isn't on its own command topic (defensive firmware behaviour).
2. `tests/test_broker_conformance.py` reproduces the bug. It's marked `xfail(strict=True)` on the embedded
   broker, so if amqtt fixes it, the test will start passing and the marker gets flagged for removal.
   Against Mosquitto it runs as a normal test.

## Flaky test hunt: two race conditions

An intermittent error (roughly 1 in 10 runs) was tracked down by looping the suite until it failed and
capturing the full traceback. It turned out to be two separate races:

1. **Broker crash (amqtt 0.12.1).** The same connect-time loop behind the leak above `await`s while iterating
   a broker-wide dict. If another client subscribes or disconnects at that moment, the broker raises
   `RuntimeError: dictionary changed size during iteration` and the connecting client never gets its SUBACK.
   `tests/helpers/broker.py` overrides that one method so it never suspends mid-loop.
2. **Device stop-during-reboot (our simulator).** After a reboot the device announced "online" before its
   telemetry thread had started, so a `stop()` arriving in that window crashed. Start, stop, crash and reboot
   now share a lifecycle lock.

Verified with 40 consecutive stress runs and no failures (before the fixes it failed within 10).

## Roadmap

- [ ] HTML test report published from CI (JUnit XML is already uploaded as an artifact)
- [ ] Network fault injection (latency, dropped connections) via a TCP proxy
- [ ] MQTT 5 features (response topic, correlation data)
