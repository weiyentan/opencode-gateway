"""Kafka consumer that reads :class:`IngestRequest` records from topic
``opencode-usage`` and POSTs them to the Gateway's ``/ingest`` endpoint.

Error handling strategy
------------------------

- **2xx** → commit Kafka offset.
- **4xx** (bad data) → produce message to DLQ topic, commit offset,
  log warning.
- **5xx / network error** → retry with exponential backoff
  (max 3 retries, base 1 s, factor 2).  Do **not** commit offset.
- **Max retries exhausted** → log error, let Kafka redeliver on the
  next poll (offset is not committed).

Graceful shutdown
-----------------

On SIGTERM/SIGINT the consumer stops polling, finishes any in-flight
HTTP POST, commits the current offset, and closes the Kafka consumer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from asyncio import Task
from dataclasses import dataclass
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import ConsumerRecord, TopicPartition
from pydantic import ValidationError

from app.api.ingest import IngestRequest

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_BACKOFF = 2.0


@dataclass(frozen=True)
class ConsumerSettings:
    """Settings owned by the Kafka consumer entry point."""

    base_url: str = ""
    collector_token: str = ""
    kafka_brokers: str = ""
    kafka_topic: str = "opencode-usage"
    kafka_dlq_topic: str = "opencode-usage-dlq"
    consumer_group_id: str = "opencode-gateway"

    @classmethod
    def from_env(cls) -> ConsumerSettings:
        """Load consumer settings from GATEWAY_* environment variables."""
        return cls(
            base_url=os.getenv("GATEWAY_BASE_URL", ""),
            collector_token=os.getenv("GATEWAY_COLLECTOR_TOKEN", ""),
            kafka_brokers=os.getenv("GATEWAY_KAFKA_BROKERS", ""),
            kafka_topic=os.getenv("GATEWAY_KAFKA_TOPIC", "opencode-usage"),
            kafka_dlq_topic=os.getenv("GATEWAY_KAFKA_DLQ_TOPIC", "opencode-usage-dlq"),
            consumer_group_id=os.getenv("GATEWAY_CONSUMER_GROUP_ID", "opencode-gateway"),
        )


class KafkaConsumer:
    """Consume usage records from Kafka and forward them to the Gateway.

    Instantiate with a :class:`Settings` object, then call :meth:`start`
    to begin the consume loop.  Call :meth:`stop` or send SIGTERM/SIGINT
    to initiate graceful shutdown.
    """

    def __init__(self, settings: ConsumerSettings) -> None:
        self._settings = settings
        self._shutdown_event = asyncio.Event()
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._poll_task: Task[None] | None = None

    # ── Public API ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Kafka consumer and begin the consume loop.

        Registers SIGTERM and SIGINT handlers for graceful shutdown.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)

        self._consumer = self._build_consumer()
        self._producer = self._build_producer()
        self._http_client = self._build_http_client()

        await self._consumer.start()
        await self._producer.start()

        logger.info(
            "Kafka consumer started: topic=%s group=%s brokers=%s",
            self._settings.kafka_topic,
            self._settings.consumer_group_id,
            self._settings.kafka_brokers,
        )

        self._poll_task = asyncio.create_task(self._poll_loop())

    async def run(self) -> None:
        """Run the consumer until shutdown, then close resources."""
        await self.start()
        try:
            if self._poll_task is not None:
                await self._poll_task
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Initiate graceful shutdown.

        Signals the poll loop to stop, waits for any in-flight POST
        to finish, commits the current offset, and closes the consumer.
        Idempotent — safe to call multiple times.
        """
        if self._shutdown_event.is_set():
            return
        logger.info("Kafka consumer shutting down...")
        self._shutdown_event.set()

        if self._poll_task is not None and self._poll_task is not asyncio.current_task():
            await self._poll_task

        if self._consumer is not None:
            await self._consumer.stop()

        if self._producer is not None:
            await self._producer.stop()

        if self._http_client is not None:
            await self._http_client.aclose()

        logger.info("Kafka consumer shut down complete.")

    # ── Internals ──────────────────────────────────────────────────────

    def _signal_handler(self) -> None:
        logger.info("Received shutdown signal")
        self._shutdown_event.set()

    def _build_consumer(self) -> AIOKafkaConsumer:
        brokers = [b.strip() for b in self._settings.kafka_brokers.split(",") if b.strip()]
        return AIOKafkaConsumer(
            self._settings.kafka_topic,
            bootstrap_servers=brokers,
            group_id=self._settings.consumer_group_id,
            value_deserializer=self._json_deserializer,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    def _build_producer(self) -> AIOKafkaProducer:
        brokers = [b.strip() for b in self._settings.kafka_brokers.split(",") if b.strip()]
        return AIOKafkaProducer(
            bootstrap_servers=brokers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )

    def _build_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.base_url,
            headers={"Authorization": f"Bearer {self._settings.collector_token}"},
            timeout=httpx.Timeout(30.0),
        )

    @staticmethod
    def _json_deserializer(raw: bytes | None) -> Any:
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    async def _poll_loop(self) -> None:
        """Continuously poll Kafka for new messages until shutdown is signalled."""
        assert self._consumer is not None
        assert self._http_client is not None

        while not self._shutdown_event.is_set():
            try:
                batch = await self._consumer.getmany(timeout_ms=1000, max_records=10)
            except Exception:
                logger.exception("Error polling Kafka")
                await asyncio.sleep(1)
                continue

            for tp, messages in batch.items():
                for msg in messages:
                    if self._shutdown_event.is_set():
                        break
                    try:
                        should_commit = await self._process_message(msg)
                    except Exception:
                        logger.exception(
                            "Failed to process message at %s:%s:%s; offset NOT committed",
                            msg.topic,
                            msg.partition,
                            msg.offset,
                        )
                        continue

                    if not should_commit:
                        break

                    # Commit after each successfully processed or DLQ'd message.
                    # Must extract TopicPartition from the record for explicit commit.
                    try:
                        tp_obj = TopicPartition(tp.topic, tp.partition)
                        await self._consumer.commit({tp_obj: msg.offset + 1})
                    except Exception:
                        logger.exception("Failed to commit offset for %s", tp)

    async def _process_message(self, msg: ConsumerRecord) -> bool:
        """Deserialize, validate, and POST a single Kafka message.

        Returns True only when the caller may commit the Kafka offset.
        """
        raw_value = msg.value
        logger.debug("Processing message: topic=%s partition=%s offset=%s",
                     msg.topic, msg.partition, msg.offset)

        # ── Deserialize and validate against IngestRequest schema ──────
        try:
            ingest_request = IngestRequest.model_validate(raw_value)
        except ValidationError as exc:
            logger.warning(
                "Message validation failed: offset=%s errors=%s",
                msg.offset,
                exc.errors(),
            )
            await self._send_to_dlq(raw_value, msg)
            return True

        # ── POST to Gateway with retry ─────────────────────────────────
        success = await self._post_with_retry(ingest_request)

        if not success:
            logger.warning(
                "Max retries exhausted for message at offset=%s — "
                "offset NOT committed; Kafka will redeliver",
                msg.offset,
            )
            return False

        return True

    async def _post_with_retry(self, ingest_request: IngestRequest) -> bool:
        """POST to ``/ingest`` with exponential-backoff retry.

        Returns ``True`` when the POST succeeds (2xx) or the message
        is sent to DLQ (4xx, handled inside ``_post_to_ingest``).
        Returns ``False`` when all retries are exhausted (5xx / network error).
        """
        assert self._http_client is not None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await self._post_to_ingest(ingest_request)
            except Exception as exc:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (_RETRY_BACKOFF ** (attempt - 1))
                    logger.warning(
                        "POST /ingest attempt %d/%d failed (%s); retrying in %.1fs",
                        attempt,
                        _MAX_RETRIES,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.exception(
                        "POST /ingest failed after %d attempts",
                        _MAX_RETRIES,
                    )

        return False

    async def _post_to_ingest(self, ingest_request: IngestRequest) -> bool:
        """Send a single POST to ``/ingest`` and classify the response.

        Returns ``True`` when the message is successfully processed
        (2xx) or routed to the DLQ (4xx).
        Raises an exception on 5xx or network errors so the retry
        loop can handle them.

        Note: 4xx handling here only applies to response-level 4xx
        (e.g. auth failures, schema version errors).  Per-record 4xx
        (validation rejections) come back as 2xx with per-record
        ``rejected`` status, so they follow the 2xx path.
        """
        assert self._http_client is not None

        payload = ingest_request.model_dump(mode="json")
        response = await self._http_client.post("/ingest", json=payload)

        if response.is_success:
            logger.debug("POST /ingest succeeded: status=%s", response.status_code)
            return True

        if 400 <= response.status_code < 500:
            # 4xx: bad request — send to DLQ, do not retry
            logger.warning(
                "POST /ingest returned %d (4xx); sending to DLQ. "
                "detail=%s",
                response.status_code,
                response.text[:500],
            )
            # Send the raw payload to DLQ; the caller already committed.
            await self._send_to_dlq(payload, None)
            return True

        # 5xx or unexpected status
        raise httpx.HTTPStatusError(
            f"Ingest returned {response.status_code}: {response.text[:500]}",
            request=response.request,
            response=response,
        )

    async def _send_to_dlq(self, payload: Any, msg: ConsumerRecord | None) -> None:
        """Produce a message to the configured DLQ topic.

        ``payload`` is the raw (dict) message value.  Metadata from
        the original ``ConsumerRecord`` (topic, partition, offset) is
        attached as headers for traceability.
        """
        assert self._producer is not None

        headers: list[tuple[str, bytes]] = []
        if msg is not None:
            headers = [
                ("source-topic", msg.topic.encode("utf-8")),
                ("source-partition", str(msg.partition).encode("utf-8")),
                ("source-offset", str(msg.offset).encode("utf-8")),
            ]

        await self._producer.send_and_wait(
            self._settings.kafka_dlq_topic,
            value=payload,
            headers=headers,
        )
        logger.info(
            "Message sent to DLQ topic=%s offset=%s",
            self._settings.kafka_dlq_topic,
            msg.offset if msg else "N/A",
        )
