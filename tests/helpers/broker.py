"""Runs an in-process MQTT broker (amqtt) on a background thread.

This lets the whole suite run with zero setup. In CI you can instead point the
tests at a real broker such as Mosquitto with MQTT_HOST / MQTT_PORT.
"""

import asyncio
import socket
import threading

from amqtt.broker import Broker


class _PatchedBroker(Broker):
    """Works around a race in amqtt 0.12.1 (version is pinned in requirements.txt).

    On every client connect, amqtt loops over the broker-wide subscription dict
    and awaits this method inside the loop. While it is suspended, another
    client can subscribe or disconnect, the dict changes size, and the broker
    raises "RuntimeError: dictionary changed size during iteration". The
    connecting client is left half-connected and never gets its SUBACK.

    This override does the same work but never suspends: it schedules the
    publishes instead of awaiting them, so the caller's loop can't be
    interrupted. The retained-message leak itself is left as-is on purpose;
    tests/test_broker_conformance.py documents it.
    """

    _pending: set[asyncio.Task] = set()

    async def _publish_retained_messages_for_subscription(self, subscription, session):
        topic_filter, qos = subscription
        handler = self._get_handler(session)
        if handler is None:
            return
        for topic, retained in list(self._retained_messages.items()):
            if self._matches(topic, topic_filter):
                task = asyncio.ensure_future(
                    handler.mqtt_publish(retained.topic, retained.data, min(qos, retained.qos or qos), retain=True)
                )
                # Keep a reference so the task isn't garbage-collected mid-flight.
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EmbeddedBroker:
    def __init__(self, host: str = "127.0.0.1", port: int | None = None):
        self.host = host
        self.port = port or free_port()
        self._loop = asyncio.new_event_loop()
        self._broker: _PatchedBroker | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        self._loop.run_forever()

    async def _start(self):
        config = {"listeners": {"default": {"type": "tcp", "bind": f"{self.host}:{self.port}"}}}
        self._broker = _PatchedBroker(config)
        await self._broker.start()
        self._ready.set()

    def start(self, timeout: float = 10) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Embedded MQTT broker did not start in time")

    def stop(self) -> None:
        if self._broker:
            asyncio.run_coroutine_threadsafe(self._broker.shutdown(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
