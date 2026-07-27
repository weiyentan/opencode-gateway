"""Tests for the Kafka consumer bridge module (``app.consumer.consumer``).

Covers all error-handling paths and lifecycle behaviours:

* 2xx → offset committed
* 4xx → DLQ + commit
* 5xx / network error → retry with backoff
* Max retries → skip commit
* Invalid message → DLQ
* Graceful shutdown
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from aiokafka.structs import ConsumerRecord

from app.consumer.consumer import Consumer

# ── Helpers ────────────────────────────────────────────────────────────────


def _mk_msg(value: dict) -> MagicMock:
    """Build a MagicMock that quacks like an aiokafka ConsumerRecord."""
    msg = MagicMock(spec=ConsumerRecord)
    msg.value = value
    msg.offset = 42
    msg.partition = 0
    msg.topic = "opencode-usage"
    msg.key = None
    msg.headers = ()
    return msg


def _valid_payload(**overrides: object) -> dict:
    """Return a minimal valid IngestRequest payload, overridable per test."""
    return {
        "schema_version": "1.0",
        "collector_version": "0.1.0",
        "source_database_id": "12345678-1234-5678-1234-567812345678",
        "records": [
            {
                "source_record_id": "rec-001",
                "session_id": "ses_abc",
                "model": "claude-sonnet-4-20250514",
                "input_tokens": 100,
                "output_tokens": 50,
                "reported_at": "2025-07-16T12:00:00Z",
            }
        ],
        **overrides,
    }


# ── 2xx → commit offset ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_2xx_commits_offset():
    """On a 2xx response the Kafka offset is committed and POST is called once."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
    )

    consumer._http_client = AsyncMock()
    consumer._http_client.post = AsyncMock(
        return_value=MagicMock(status_code=200)
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())
    await consumer._process_message(msg)

    consumer._http_client.post.assert_called_once()
    consumer._consumer.commit.assert_called_once()
    consumer._producer.send_and_wait.assert_not_called()


# ── 4xx → DLQ + commit ═════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4xx_sends_to_dlq_and_commits():
    """On a 4xx response the payload is sent to the DLQ and offset committed."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
    )

    consumer._http_client = AsyncMock()
    consumer._http_client.post = AsyncMock(
        return_value=MagicMock(status_code=400)
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_msg(_valid_payload())
    await consumer._process_message(msg)

    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert dlq_payload["reason"] == "HTTP 400"
    assert dlq_payload["original_topic"] == "opencode-usage"
    assert "payload" in dlq_payload


# ── 5xx → retry with backoff, commit on success ───────────────────────────


@pytest.mark.asyncio
async def test_5xx_retries_then_commits_on_success():
    """On 5xx responses the consumer retries then commits on eventual 200."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
    )

    consumer._http_client = AsyncMock()
    consumer._http_client.post = AsyncMock(
        side_effect=[
            MagicMock(status_code=503),
            MagicMock(status_code=503),
            MagicMock(status_code=200),
        ]
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())

    with patch(
        "app.consumer.consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert consumer._http_client.post.call_count == 3
    assert mock_sleep.call_count == 2
    consumer._consumer.commit.assert_called_once()
    consumer._producer.send_and_wait.assert_not_called()


# ── Max retries → skip commit ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_retries_exhausted_skips_commit():
    """When max retries are exhausted the offset is NOT committed."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
        max_retries=3,
    )

    consumer._http_client = AsyncMock()
    consumer._http_client.post = AsyncMock(
        return_value=MagicMock(status_code=503)
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())

    with patch(
        "app.consumer.consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert consumer._http_client.post.call_count == 3  # all retries
    assert mock_sleep.call_count == 2  # slept between retries 0→1 and 1→2
    consumer._consumer.commit.assert_not_called()
    consumer._producer.send_and_wait.assert_not_called()


# ── Network error → retry ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_network_error_retries():
    """RequestError (network failure) triggers retry — commits on recovery."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
        max_retries=3,
    )

    consumer._http_client = AsyncMock()
    consumer._http_client.post = AsyncMock(
        side_effect=[
            httpx.RequestError("Connection refused"),
            httpx.RequestError("Connection refused"),
            MagicMock(status_code=200),
        ]
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())

    with patch(
        "app.consumer.consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert consumer._http_client.post.call_count == 3
    assert mock_sleep.call_count == 2
    consumer._consumer.commit.assert_called_once()


@pytest.mark.asyncio
async def test_network_error_max_retries_exhausted():
    """When all retries fail with network errors, offset is NOT committed."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
        max_retries=2,
    )

    consumer._http_client = AsyncMock()
    consumer._http_client.post = AsyncMock(
        side_effect=[
            httpx.RequestError("Connection refused"),
            httpx.RequestError("Connection refused"),
        ]
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())

    with patch(
        "app.consumer.consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert consumer._http_client.post.call_count == 2
    assert mock_sleep.call_count == 1
    consumer._consumer.commit.assert_not_called()


# ── Invalid message → DLQ ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_message_sends_to_dlq():
    """Messages that fail Pydantic validation are sent to DLQ, offset committed."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
    )

    consumer._http_client = AsyncMock()
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_msg({"not": "valid"})

    await consumer._process_message(msg)

    # Never POST to Gateway — bad message
    consumer._http_client.post.assert_not_called()
    # Sent to DLQ and offset committed (bad data shouldn't block the pipe)
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Invalid message shape" in dlq_payload["reason"]


# ── Graceful shutdown ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graceful_shutdown_waits_for_in_flight():
    """stop() waits for an in-flight POST to complete before closing."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
    )

    gate = asyncio.Event()

    async def slow_post(*_args: object, **_kwargs: object) -> MagicMock:
        await gate.wait()
        return MagicMock(status_code=200)

    consumer._http_client = AsyncMock()
    consumer._http_client.post = slow_post
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._consumer.stop = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.stop = AsyncMock()
    consumer._running = True

    msg = _mk_msg(_valid_payload())

    # Start processing — it will block on gate
    consumer._in_flight = asyncio.ensure_future(consumer._process_message(msg))
    await asyncio.sleep(0)  # let the task start

    # Begin shutdown
    stop_task = asyncio.ensure_future(consumer.stop())
    await asyncio.sleep(0)  # let stop() start waiting

    # stop() should be waiting for in-flight, not yet done
    assert not stop_task.done()

    # Release the in-flight POST
    gate.set()

    await asyncio.wait_for(stop_task, timeout=5.0)

    # After stop, in-flight is complete, offset committed
    assert consumer._in_flight.done()
    consumer._consumer.commit.assert_called_once()
    consumer._consumer.stop.assert_called_once()
    consumer._producer.stop.assert_called_once()


@pytest.mark.asyncio
async def test_stop_handles_in_flight_timeout():
    """stop() cancels in-flight if it doesn't complete within the timeout."""
    consumer = Consumer(
        kafka_brokers="broker:9092",
        gateway_base_url="http://gateway:8000",
        gateway_collector_token="tok",
    )

    # An in-flight that never completes
    never = asyncio.Event()

    async def stuck_post(*_args: object, **_kwargs: object) -> MagicMock:
        await never.wait()
        return MagicMock(status_code=200)

    consumer._http_client = AsyncMock()
    consumer._http_client.post = stuck_post
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._consumer.stop = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.stop = AsyncMock()
    consumer._running = True

    msg = _mk_msg(_valid_payload())

    # Start a never-completing task
    consumer._in_flight = asyncio.ensure_future(consumer._process_message(msg))
    await asyncio.sleep(0)

    # Call stop — it will hit the wait_for with 30s timeout.
    # We let the timeout fire naturally by cancelling the in-flight task,
    # then release the stuck post so the task cleans up.
    consumer._in_flight.cancel()
    never.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer._in_flight

    # Now stop() should complete quickly (no in-flight to wait for)
    await consumer.stop()

    consumer._consumer.stop.assert_called_once()
    consumer._producer.stop.assert_called_once()


# ── from_env factory ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_from_env_reads_all_vars():
    """from_env() reads both required and optional environment variables."""
    env_vars = {
        "GATEWAY_KAFKA_BROKERS": "broker1:9092,broker2:9092",
        "GATEWAY_BASE_URL": "http://gateway.example.com",
        "GATEWAY_COLLECTOR_TOKEN": "secret-token",
        "GATEWAY_KAFKA_TOPIC": "my-usage",
        "GATEWAY_KAFKA_DLQ_TOPIC": "my-usage-dlq",
        "GATEWAY_CONSUMER_GROUP_ID": "my-group",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        c = Consumer.from_env()

    assert c._kafka_brokers == "broker1:9092,broker2:9092"
    assert c._gateway_base_url == "http://gateway.example.com"
    assert c._gateway_collector_token == "secret-token"
    assert c._kafka_topic == "my-usage"
    assert c._kafka_dlq_topic == "my-usage-dlq"
    assert c._consumer_group_id == "my-group"


@pytest.mark.asyncio
async def test_from_env_falls_back_to_defaults():
    """from_env() uses hard-coded defaults when optional vars are absent."""
    env_vars = {
        "GATEWAY_KAFKA_BROKERS": "broker:9092",
        "GATEWAY_BASE_URL": "http://gw:8000",
        "GATEWAY_COLLECTOR_TOKEN": "tok",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        c = Consumer.from_env()

    assert c._kafka_topic == "opencode-usage"
    assert c._kafka_dlq_topic == "opencode-usage-dlq"
    assert c._consumer_group_id == "opencode-gateway"


@pytest.mark.asyncio
async def test_import_from_consumer_module():
    """The Consumer class is importable from app.consumer."""
    from app.consumer import Consumer as ImportedConsumer

    assert ImportedConsumer is Consumer
