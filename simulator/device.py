"""A simulated smart thermostat that talks MQTT.

This is the "device under test" (DUT). In a real job this would be firmware
running on hardware; here it is a Python class so we can run it anywhere.

Topic contract (base = devices/<device_id>):

    <base>/status          retained  {"state": "online" | "offline", "fw": "..."}
    <base>/telemetry       QoS 0     periodic sensor readings
    <base>/cmd             QoS 1     commands sent TO the device
    <base>/cmd/response    QoS 1     replies FROM the device

Command payload:   {"id": "<request id>", "action": "<name>", "params": {...}}
Response payload:  {"id": "<same id>", "ok": true|false, "result": {...}, "error": "..."|null}
"""

from __future__ import annotations

import json
import random
import threading
import time

import paho.mqtt.client as mqtt

FIRMWARE_VERSION = "1.4.2"

AMBIENT_C = 20.0
MIN_INTERVAL_S, MAX_INTERVAL_S = 0.05, 60.0
MIN_SETPOINT_C, MAX_SETPOINT_C = 5.0, 35.0


class CommandError(Exception):
    """Raised by a command handler when the request is invalid."""


class ThermostatSimulator:
    def __init__(
        self,
        device_id: str,
        host: str = "127.0.0.1",
        port: int = 1883,
        interval_s: float = 1.0,
        setpoint_c: float = 21.0,
        seed: int | None = None,
    ):
        self.device_id = device_id
        self.host = host
        self.port = port
        self.base = f"devices/{device_id}"

        self._lock = threading.Lock()
        self._interval_s = interval_s
        self._setpoint_c = setpoint_c
        self._temperature_c = AMBIENT_C
        self._humidity_pct = 45.0
        self._heater_on = False
        self._seq = 0
        self._fault: str | None = None
        self._rng = random.Random(seed)

        self._stop = threading.Event()
        self._telemetry_thread: threading.Thread | None = None
        self._client = self._make_client()

    # ------------------------------------------------------------------ lifecycle

    def _make_client(self) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{self.device_id}")
        # Last Will and Testament: the BROKER publishes this if we vanish without
        # a clean DISCONNECT (power loss, crash, network drop).
        client.will_set(f"{self.base}/status", self._status_payload("offline"), qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        return client

    def start(self) -> None:
        self._stop.clear()
        self._client.connect(self.host, self.port, keepalive=10)
        self._client.loop_start()
        self._telemetry_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
        self._telemetry_thread.start()

    def stop(self) -> None:
        """Graceful shutdown: announce offline, then disconnect cleanly. Safe to call twice."""
        if not self._client.is_connected():
            self._stop.set()
            self._client.loop_stop()
            return
        self._stop.set()
        if self._telemetry_thread:
            self._telemetry_thread.join(timeout=2)
        info = self._client.publish(f"{self.base}/status", self._status_payload("offline"), qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=2)
        except RuntimeError:
            pass  # already disconnected (e.g. after crash()), nothing to announce
        self._client.disconnect()
        self._client.loop_stop()

    def crash(self) -> None:
        """Simulate power loss: drop the socket with no DISCONNECT packet.

        The broker should notice and publish our Last Will.
        """
        self._stop.set()
        self._client.loop_stop()
        sock = self._client.socket()
        if sock:
            sock.close()

    # ------------------------------------------------------------------ MQTT callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        client.subscribe(f"{self.base}/cmd", qos=1)
        client.publish(f"{self.base}/status", self._status_payload("online"), qos=1, retain=True)

    def _on_message(self, client, userdata, msg):
        # Defensive: only act on our own command topic. A misbehaving broker can
        # deliver messages we never subscribed to (see tests/test_broker_conformance.py).
        if msg.topic != f"{self.base}/cmd":
            return
        try:
            request = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(None, ok=False, error="malformed_json")
            return

        if not isinstance(request, dict):
            self._respond(None, ok=False, error="malformed_json")
            return

        request_id = request.get("id")
        action = request.get("action")
        params = request.get("params") or {}

        handler = self._handlers().get(action)
        if handler is None:
            self._respond(request_id, ok=False, error=f"unknown_action:{action}")
            return
        try:
            result = handler(params)
        except CommandError as exc:
            self._respond(request_id, ok=False, error=str(exc))
            return
        self._respond(request_id, ok=True, result=result)

        if action == "reboot":
            threading.Thread(target=self._reboot, daemon=True).start()

    def _respond(self, request_id, ok: bool, result: dict | None = None, error: str | None = None):
        payload = {"id": request_id, "ok": ok, "result": result or {}, "error": error}
        self._client.publish(f"{self.base}/cmd/response", json.dumps(payload), qos=1)

    # ------------------------------------------------------------------ commands

    def _handlers(self):
        return {
            "ping": self._cmd_ping,
            "get_state": self._cmd_get_state,
            "set_interval": self._cmd_set_interval,
            "set_setpoint": self._cmd_set_setpoint,
            "inject_fault": self._cmd_inject_fault,
            "clear_fault": self._cmd_clear_fault,
            "reboot": self._cmd_reboot,
        }

    def _cmd_ping(self, params):
        return {"pong": True}

    def _cmd_get_state(self, params):
        with self._lock:
            return {
                "interval_s": self._interval_s,
                "setpoint_c": self._setpoint_c,
                "fault": self._fault,
                "fw": FIRMWARE_VERSION,
            }

    def _cmd_set_interval(self, params):
        seconds = _require_number(params, "seconds")
        if not MIN_INTERVAL_S <= seconds <= MAX_INTERVAL_S:
            raise CommandError("out_of_range:seconds")
        with self._lock:
            self._interval_s = float(seconds)
        return {"interval_s": float(seconds)}

    def _cmd_set_setpoint(self, params):
        celsius = _require_number(params, "celsius")
        if not MIN_SETPOINT_C <= celsius <= MAX_SETPOINT_C:
            raise CommandError("out_of_range:celsius")
        with self._lock:
            self._setpoint_c = float(celsius)
        return {"setpoint_c": float(celsius)}

    def _cmd_inject_fault(self, params):
        fault = params.get("type")
        if fault not in ("sensor_disconnected",):
            raise CommandError("invalid_param:type")
        with self._lock:
            self._fault = fault
        return {"fault": fault}

    def _cmd_clear_fault(self, params):
        with self._lock:
            self._fault = None
        return {"fault": None}

    def _cmd_reboot(self, params):
        return {"rebooting": True}

    def _reboot(self):
        # Give the response a moment to leave before we drop the connection.
        time.sleep(0.1)
        self._stop.set()
        if self._telemetry_thread:
            self._telemetry_thread.join(timeout=2)
        self._client.publish(f"{self.base}/status", self._status_payload("offline"), qos=1, retain=True).wait_for_publish(2)
        self._client.disconnect()
        self._client.loop_stop()
        with self._lock:
            self._seq = 0
            self._fault = None
        time.sleep(0.2)
        self._client = self._make_client()
        self.start()

    # ------------------------------------------------------------------ telemetry

    def _telemetry_loop(self):
        while not self._stop.is_set():
            self._client.publish(f"{self.base}/telemetry", json.dumps(self._next_reading()), qos=0)
            with self._lock:
                interval = self._interval_s
            self._stop.wait(interval)

    def _next_reading(self) -> dict:
        with self._lock:
            # Simple physics: the heater warms the room, otherwise it drifts to ambient.
            self._heater_on = self._temperature_c < self._setpoint_c - 0.5
            if self._heater_on:
                self._temperature_c += 0.2
            elif self._temperature_c > AMBIENT_C:
                self._temperature_c -= 0.1
            self._humidity_pct = min(100.0, max(0.0, self._humidity_pct + self._rng.uniform(-0.5, 0.5)))

            reading = {
                "device_id": self.device_id,
                "seq": self._seq,
                "ts": time.time(),
                "temperature_c": round(self._temperature_c + self._rng.uniform(-0.05, 0.05), 2),
                "humidity_pct": round(self._humidity_pct, 1),
                "setpoint_c": self._setpoint_c,
                "heater_on": self._heater_on,
                "fault": self._fault,
            }
            if self._fault == "sensor_disconnected":
                reading["temperature_c"] = None
                reading["heater_on"] = False  # fail safe: never heat blind
            self._seq += 1
            return reading

    def _status_payload(self, state: str) -> str:
        return json.dumps({"state": state, "fw": FIRMWARE_VERSION})


def _require_number(params: dict, key: str) -> float:
    value = params.get(key)
    # bool is a subclass of int in Python, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError(f"invalid_param:{key}")
    return value
