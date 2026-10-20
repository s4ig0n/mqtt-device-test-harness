"""Checks on the broker itself, not the device.

While building this suite, a device started answering commands nobody sent.
The cause was the embedded broker (amqtt 0.12.1): when a client connects, it
replays retained messages matching *every* client's subscriptions instead of
only the new client's own. MQTT 3.1.1 section 3.3.1.3 says retained messages
are sent for the subscriber's own matching subscriptions only.

These tests pin that behaviour down. They are marked xfail on the embedded
broker (known bug) and are expected to pass against Mosquitto.
"""

import os
import uuid

import pytest

from tests.helpers.probe import MqttProbe

USING_EMBEDDED_BROKER = not os.environ.get("MQTT_HOST")

known_amqtt_bug = pytest.mark.xfail(
    USING_EMBEDDED_BROKER,
    reason="amqtt 0.12.1 replays retained messages for other clients' subscriptions on connect",
    strict=True,  # if the bug gets fixed upstream, this starts failing so we notice and remove the marker
)


@known_amqtt_bug
def test_new_client_only_receives_retained_messages_it_subscribed_to(broker):
    host, port = broker
    other_topic = f"conformance/{uuid.uuid4().hex}/status"
    my_topic = f"conformance/{uuid.uuid4().hex}/cmd"

    # Someone else subscribes to a wildcard and a retained message exists there.
    watcher = MqttProbe(host, port).connect()
    watcher.subscribe(other_topic.rsplit("/", 1)[0] + "/#")
    watcher.publish(other_topic, b"retained-for-someone-else", retain=True)

    newcomer = MqttProbe(host, port).connect()
    try:
        newcomer.subscribe(my_topic)
        # The leak happens during CONNECT, so check everything ever received, not just after a bookmark.
        newcomer.assert_no_message("#", within=0.5)
        leaked = [m.topic for m in newcomer.messages()]
        assert leaked == [], f"received retained messages it never subscribed to: {leaked}"
    finally:
        newcomer.disconnect()
        watcher.publish(other_topic, b"", retain=True)  # empty retained payload clears it
        watcher.disconnect()


def test_retained_message_is_delivered_to_matching_subscriber(broker):
    """Sanity check of the normal case, which works on both brokers."""
    host, port = broker
    topic = f"conformance/{uuid.uuid4().hex}/status"
    publisher = MqttProbe(host, port).connect()
    publisher.publish(topic, b"hello", retain=True)

    subscriber = MqttProbe(host, port).connect()
    try:
        subscriber.subscribe(topic)
        msg = subscriber.wait_for(topic)
        assert msg.payload == b"hello"
        assert msg.retain is True
    finally:
        subscriber.disconnect()
        publisher.publish(topic, b"", retain=True)
        publisher.disconnect()
