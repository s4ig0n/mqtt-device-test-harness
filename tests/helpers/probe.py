"""MqttProbe: the test's own MQTT client.

Tests never sleep-and-hope. They subscribe up front, record everything the
broker delivers, and *wait for a condition with a timeout*. That is the
single most important pattern for testing anything asynchronous.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import paho.mqtt.client as mqtt


@dataclass
class Message:
    topic: str
    payload: bytes
    qos: int
    retain: bool
    received_at: float

    def json(self):
        return json.loads(self.payload)


class MqttTimeout(AssertionError):
    """Raised when an expected message never arrives. Subclasses AssertionError
    so pytest reports it as a test failure, not an error."""


class MqttProbe:
    def __init__(self, host: str, port: int, client_id: str | None = None):
        self.host = host
        self.port = port
        self._messages: list[Message] = []
        self._cond = threading.Condition()
        self._connected = threading.Event()
        self._acked_mids: set[int] = set()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id or f"probe-{uuid.uuid4().hex[:8]}"
        )
        self.client.on_connect = lambda *a: self._connected.set()
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

    # ------------------------------------------------------------------ lifecycle

    def connect(self, timeout: float = 5) -> "MqttProbe":
        self.client.connect(self.host, self.port)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            raise MqttTimeout(f"Probe could not connect to {self.host}:{self.port}")
        return self

    def disconnect(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_message(self, client, userdata, msg):
        with self._cond:
            self._messages.append(Message(msg.topic, msg.payload, msg.qos, msg.retain, time.monotonic()))
            self._cond.notify_all()

    # ------------------------------------------------------------------ subscribe / publish

    def subscribe(self, topic: str, qos: int = 1, timeout: float = 5) -> None:
        """Subscribe and block until the broker acknowledges (SUBACK)."""
        _, mid = self.client.subscribe(topic, qos=qos)
        with self._cond:
            if not self._cond.wait_for(lambda: mid in self._acked_mids, timeout):
                raise MqttTimeout(f"No SUBACK for {topic}")

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        with self._cond:
            self._acked_mids.add(mid)
            self._cond.notify_all()

    def publish(self, topic: str, payload, qos: int = 1, retain: bool = False) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=qos, retain=retain).wait_for_publish(timeout=5)

    # ------------------------------------------------------------------ querying

    def messages(self, topic_filter: str = "#") -> list[Message]:
        with self._cond:
            return [m for m in self._messages if mqtt.topic_matches_sub(topic_filter, m.topic)]

    def clear(self) -> None:
        with self._cond:
            self._messages.clear()

    def wait_for(
        self,
        topic_filter: str,
        predicate: Callable[[Message], bool] = lambda m: True,
        timeout: float = 5,
        start: int = 0,
    ) -> Message:
        """Return the first message matching topic + predicate, or fail after timeout.

        `start` lets you ignore messages received before a given point
        (use `len(probe.messages())` as a bookmark).
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self._messages[start:]:
                    if mqtt.topic_matches_sub(topic_filter, m.topic) and predicate(m):
                        return m
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    seen = [m.topic for m in self._messages[start:]][-10:]
                    raise MqttTimeout(
                        f"No message on '{topic_filter}' matched within {timeout}s. Last topics seen: {seen}"
                    )
                self._cond.wait(remaining)

    def wait_for_count(self, topic_filter: str, count: int, timeout: float = 5, start: int = 0) -> list[Message]:
        """Wait until `count` messages on the topic have arrived (after bookmark `start`)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                matched = [m for m in self._messages[start:] if mqtt.topic_matches_sub(topic_filter, m.topic)]
                if len(matched) >= count:
                    return matched[:count]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MqttTimeout(f"Expected {count} messages on '{topic_filter}', got {len(matched)}")
                self._cond.wait(remaining)

    def assert_no_message(self, topic_filter: str, within: float) -> None:
        """Negative check: fail if anything arrives on the topic during the window."""
        bookmark = len(self._messages)
        time.sleep(within)
        unexpected = [m for m in self._messages[bookmark:] if mqtt.topic_matches_sub(topic_filter, m.topic)]
        assert not unexpected, f"Expected silence on '{topic_filter}', got {len(unexpected)} message(s)"

    def bookmark(self) -> int:
        """Index to pass as `start=` so later waits ignore older messages."""
        with self._cond:
            return len(self._messages)


class DeviceClient:
    """Thin request/response layer over the probe for one device's command topics."""

    def __init__(self, probe: MqttProbe, device_id: str):
        self.probe = probe
        self.base = f"devices/{device_id}"

    def send(self, action: str, params: dict | None = None, timeout: float = 5) -> dict:
        request_id = uuid.uuid4().hex
        self.probe.publish(f"{self.base}/cmd", {"id": request_id, "action": action, "params": params or {}})
        msg = self.probe.wait_for(
            f"{self.base}/cmd/response", lambda m: m.json().get("id") == request_id, timeout=timeout
        )
        return msg.json()
