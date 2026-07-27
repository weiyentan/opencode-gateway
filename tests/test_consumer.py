"""Tests for the Kafka consumer module.

Covers:
- Successful POST (2xx) → offset committed
- 4xx response → message sent to DLQ, offset committed
- 5xx / network error → retry with exponential backoff, no offset commit
- Max retries exhausted → log error, no commit
- Graceful shutdown
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiokafka.structs import ConsumerRecord

from app.consumer.consumer import _MAX_RETRIES, KafkaConsumer

# ── Helpers ────────────────────────────────────────────────────────────────


def _mk_settings(**overrides: str) -> Any:
    """Build a Settings-like object with consumer fields set."""
    from app.core.config import Settings

    kwargs: dict[str, str] = {
        "base_url": "http://gateway:8000",
        "collector_token": "test-collector-token",
        "kafka_brokers": "kafka:9092",
        "kafka_topic": "opencode-usage",
        "kafka_dlq_topic": "opencode-usage-dlq",
        "consumer_group_id": "opencode-gateway",
        **overrides,
    }
    return Settings(
        env="development",
        api_key="",
        **kwargs,
    )


def _mk_consumer_record(
    topic: str = "opencode-usage",
    partition: int = 0,
    offset: int = 42,
    value: Any = None,
) -> ConsumerRecord:
    """Build a fake aiokafka ConsumerRecord for testing."""
    if value is None:
        value = {}
    return ConsumerRecord(
        topic=topic,
        partition=partition,
        offset=offset,
        timestamp=1700000000000,
        timestamp_type=0,
        key=None,
        value=value,
        checksum=None,
        serialized_key_size=-1,
        serialized_value_size=-1,
        headers=(),
    )


def _mk_ingest_payload(
    schema_version: str = "1.0",
    collector_version: str = "0.1.0",
    source_database_id: uuid.UUID | None = None,
    records: list[dict] | None = None,
) -> dict[str, Any]:
    """Return a valid ingest request dict (raw JSON, as from Kafka)."""
    if source_database_id is None:
        source_database_id = uuid.uuid4()
    if records is None:
        records = [
            {
                "source_record_id": "rec-001",
                "session_id": "ses_abc123",
                "model": "gpt-4o",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": 0.01,
                "reported_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
            }
        ]
    return {
        "schema_version": schema_version,
        "collector_version": collector_version,
        "source_database_id": str(source_database_id),
        "records": records,
        "session_contexts": [],
        "projects": [],
        "project_directories": [],
        "session_todos": [],
    }


def _mk_2xx_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.is_success = True
    resp.status_code = 200
    resp.text = '{"batch_id": "..."}'
    resp.request = MagicMock()
    return resp


def _mk_4xx_response(status: int = 400) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.is_success = False
    resp.status_code = status
    resp.text = '{"detail": "Bad request"}'
    resp.request = MagicMock()
    return resp


def _mk_5xx_response(status: int = 500) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.is_success = False
    resp.status_code = status
    resp.text = "Internal server error"
    resp.request = MagicMock()
    return resp


# ── Config tests ───────────────────────────────────────────────────────────


def test_consumer_config_defaults(monkeypatch):
    """Kafka consumer settings should have sensible defaults."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    from app.core.config import Settings

    settings = Settings()
    assert settings.kafka_topic == "opencode-usage"
    assert settings.kafka_dlq_topic == "opencode-usage-dlq"
    assert settings.consumer_group_id == "opencode-gateway"
    assert settings.base_url == ""
    assert settings.collector_token == ""
    assert settings.kafka_brokers == ""


def test_consumer_config_env_override(monkeypatch):
    """GATEWAY_KAFKA_* env vars should override defaults."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_KAFKA_TOPIC", "my-topic")
    monkeypatch.setenv("GATEWAY_KAFKA_DLQ_TOPIC", "my-dlq")
    monkeypatch.setenv("GATEWAY_CONSUMER_GROUP_ID", "my-group")
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gw:9000")

    from app.core.config import Settings

    settings = Settings()
    assert settings.kafka_topic == "my-topic"
    assert settings.kafka_dlq_topic == "my-dlq"
    assert settings.consumer_group_id == "my-group"
    assert settings.base_url == "http://gw:9000"


# ── Process-message tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_message_2xx_commits(caplog):
    """On 2xx POST, the consumer should accept the message (caller commits offset)."""
    caplog.set_level(logging.DEBUG)

    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = _mk_2xx_response()
    consumer._http_client = http_client

    mock_consumer = AsyncMock()
    mock_consumer.commit = AsyncMock()
    consumer._consumer = mock_consumer

    mock_producer = AsyncMock()
    mock_producer.send_and_wait = AsyncMock()
    consumer._producer = mock_producer

    payload = _mk_ingest_payload()
    record = _mk_consumer_record(value=payload)

    await consumer._process_message(record)

    http_client.post.assert_called_once()
    call_args = http_client.post.call_args
    assert call_args[0][0] == "/ingest"
    # The DLQ producer should NOT have been called
    mock_producer.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_process_message_4xx_sends_to_dlq(caplog):
    """On 4xx POST, the message should be sent to DLQ."""
    caplog.set_level(logging.DEBUG)

    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = _mk_4xx_response(400)
    consumer._http_client = http_client

    mock_consumer = AsyncMock()
    consumer._consumer = mock_consumer

    mock_producer = AsyncMock()
    mock_producer.send_and_wait = AsyncMock()
    consumer._producer = mock_producer

    payload = _mk_ingest_payload()
    record = _mk_consumer_record(value=payload)

    await consumer._process_message(record)

    # Should have POSTed to /ingest
    http_client.post.assert_called_once()
    # Should have sent to DLQ (model_dump'd payload, which adds defaults)
    mock_producer.send_and_wait.assert_called_once()
    dlq_call = mock_producer.send_and_wait.call_args
    assert dlq_call[1]["value"]["schema_version"] == payload["schema_version"]
    assert dlq_call[1]["value"]["records"][0]["source_record_id"] == "rec-001"


@pytest.mark.asyncio
async def test_process_message_validation_failure_sends_to_dlq(caplog):
    """Invalid JSON (not a valid IngestRequest) should be sent to DLQ."""
    caplog.set_level(logging.WARNING)

    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    consumer._http_client = http_client

    mock_consumer = AsyncMock()
    consumer._consumer = mock_consumer

    mock_producer = AsyncMock()
    mock_producer.send_and_wait = AsyncMock()
    consumer._producer = mock_producer

    # Invalid payload (missing required fields)
    bad_payload: dict[str, Any] = {"not": "valid"}
    record = _mk_consumer_record(value=bad_payload)

    await consumer._process_message(record)

    # Should NOT have POSTed (validation failed before POST)
    http_client.post.assert_not_called()
    # Should have sent raw bad payload to DLQ
    mock_producer.send_and_wait.assert_called_once()


@pytest.mark.asyncio
async def test_post_with_retry_success_on_first_try():
    """2xx response should return True on first attempt."""
    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = _mk_2xx_response()
    consumer._http_client = http_client

    from app.api.ingest import IngestRequest

    payload = _mk_ingest_payload()
    ingest_request = IngestRequest.model_validate(payload)

    result = await consumer._post_with_retry(ingest_request)
    assert result is True
    assert http_client.post.call_count == 1


@pytest.mark.asyncio
async def test_post_with_retry_5xx_retries_then_fails(caplog):
    """5xx should retry with backoff, then return False after max retries."""
    caplog.set_level(logging.WARNING)

    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = httpx.HTTPStatusError(
        "Server error",
        request=MagicMock(),
        response=_mk_5xx_response(500),
    )
    consumer._http_client = http_client

    from app.api.ingest import IngestRequest

    payload = _mk_ingest_payload()
    ingest_request = IngestRequest.model_validate(payload)

    result = await consumer._post_with_retry(ingest_request)
    assert result is False
    assert http_client.post.call_count == _MAX_RETRIES  # 3 attempts


@pytest.mark.asyncio
async def test_post_with_retry_network_error_retries(caplog):
    """Network errors (httpx.ConnectError) should be retried."""
    caplog.set_level(logging.WARNING)

    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = httpx.ConnectError("Connection refused")
    consumer._http_client = http_client

    from app.api.ingest import IngestRequest

    payload = _mk_ingest_payload()
    ingest_request = IngestRequest.model_validate(payload)

    result = await consumer._post_with_retry(ingest_request)
    assert result is False
    assert http_client.post.call_count == _MAX_RETRIES


@pytest.mark.asyncio
async def test_post_to_ingest_4xx_returns_true_and_sends_to_dlq():
    """4xx response should return True (handled) and send payload to DLQ."""
    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = _mk_4xx_response(400)
    consumer._http_client = http_client

    mock_producer = AsyncMock()
    mock_producer.send_and_wait = AsyncMock()
    consumer._producer = mock_producer

    from app.api.ingest import IngestRequest

    payload = _mk_ingest_payload()
    ingest_request = IngestRequest.model_validate(payload)

    result = await consumer._post_to_ingest(ingest_request)
    assert result is True
    # DLQ should have been called with the raw JSON payload
    mock_producer.send_and_wait.assert_called_once()


@pytest.mark.asyncio
async def test_process_message_5xx_retries_exhausted(caplog):
    """When retries are exhausted, no DLQ send and caller logs."""
    caplog.set_level(logging.WARNING)

    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = httpx.HTTPStatusError(
        "Server error",
        request=MagicMock(),
        response=_mk_5xx_response(500),
    )
    consumer._http_client = http_client

    mock_consumer = AsyncMock()
    consumer._consumer = mock_consumer

    mock_producer = AsyncMock()
    mock_producer.send_and_wait = AsyncMock()
    consumer._producer = mock_producer

    payload = _mk_ingest_payload()
    record = _mk_consumer_record(value=payload)

    await consumer._process_message(record)

    # POST was called 3 times (retries)
    assert http_client.post.call_count == _MAX_RETRIES
    # DLQ should NOT have been called (5xx, not 4xx)
    mock_producer.send_and_wait.assert_not_called()

    log_lines = [r.message for r in caplog.records]
    assert any("Max retries exhausted" in msg for msg in log_lines)


# ── Graceful shutdown tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_idempotent():
    """stop() should be safe to call multiple times."""
    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    mock_kafka_consumer = AsyncMock()
    mock_kafka_consumer.commit = AsyncMock()
    mock_kafka_consumer.stop = AsyncMock()
    consumer._consumer = mock_kafka_consumer

    mock_producer = AsyncMock()
    mock_producer.stop = AsyncMock()
    consumer._producer = mock_producer

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.aclose = AsyncMock()
    consumer._http_client = http_client

    await consumer.stop()
    await consumer.stop()  # second call should be a no-op

    # commit and stop should only be called once
    assert mock_kafka_consumer.commit.call_count == 1
    assert mock_kafka_consumer.stop.call_count == 1


@pytest.mark.asyncio
async def test_stop_closes_resources():
    """stop() should commit offsets and close consumer, producer, and HTTP client."""
    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    mock_kafka_consumer = AsyncMock()
    mock_kafka_consumer.commit = AsyncMock()
    mock_kafka_consumer.stop = AsyncMock()
    consumer._consumer = mock_kafka_consumer

    mock_producer = AsyncMock()
    mock_producer.stop = AsyncMock()
    consumer._producer = mock_producer

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.aclose = AsyncMock()
    consumer._http_client = http_client

    await consumer.stop()

    mock_kafka_consumer.commit.assert_called_once()
    mock_kafka_consumer.stop.assert_called_once()
    mock_producer.stop.assert_called_once()
    http_client.aclose.assert_called_once()


# ── DLQ header tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dlq_includes_source_metadata():
    """DLQ messages should carry source topic/partition/offset as headers."""
    settings = _mk_settings()
    consumer = KafkaConsumer(settings)

    mock_producer = AsyncMock()
    mock_producer.send_and_wait = AsyncMock()
    consumer._producer = mock_producer

    payload = {"test": "data"}
    record = _mk_consumer_record(topic="opencode-usage", partition=2, offset=99)

    await consumer._send_to_dlq(payload, record)

    mock_producer.send_and_wait.assert_called_once()
    call_kwargs = mock_producer.send_and_wait.call_args
    headers = call_kwargs[1]["headers"]
    header_dict = {k: v.decode("utf-8") for k, v in headers}
    assert header_dict["source-topic"] == "opencode-usage"
    assert header_dict["source-partition"] == "2"
    assert header_dict["source-offset"] == "99"


# ── Entry point tests ──────────────────────────────────────────────────────


def test_main_missing_kafka_brokers(capsys, monkeypatch):
    """main() should exit with error when GATEWAY_KAFKA_BROKERS is not set."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GATEWAY_KAFKA_BROKERS", "")  # empty

    from app.consumer.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


def test_main_missing_base_url(capsys, monkeypatch):
    """main() should exit with error when GATEWAY_BASE_URL is not set."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_KAFKA_BROKERS", "kafka:9092")

    from app.consumer.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
