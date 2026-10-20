"""Runs an in-process MQTT broker (amqtt) on a background thread.

This lets the whole suite run with zero setup. In CI you can instead point the
tests at a real broker such as Mosquitto with MQTT_HOST / MQTT_PORT.
"""

import asyncio
import socket
import threading

from amqtt.broker import Broker


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EmbeddedBroker:
    def __init__(self, host: str = "127.0.0.1", port: int | None = None):
        self.host = host
        self.port = port or free_port()
        self._loop = asyncio.new_event_loop()
        self._broker: Broker | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        self._loop.run_forever()

    async def _start(self):
        config = {"listeners": {"default": {"type": "tcp", "bind": f"{self.host}:{self.port}"}}}
        self._broker = Broker(config)
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
