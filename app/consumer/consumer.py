"""Kafka consumer that bridges usage records to the Gateway ingest API.

Reads JSON-serialised ``IngestRequest`` payloads from the configured Kafka
topic and POSTs each one to the Gateway's ``/ingest`` endpoint.  This
module is designed to run as a separate container alongside the Gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord

from app.consumer.models import IngestRequest

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────

_DEFAULT_KAFKA_TOPIC = "opencode-usage"
_DEFAULT_KAFKA_DLQ_TOPIC = "opencode-usage-dlq"
_DEFAULT_CONSUMER_GROUP_ID = "opencode-gateway"

_MAX_RETRIES = 5
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0


class Consumer:
    """Consumes usage records from Kafka and forwards them to the Gateway.

    Error-handling behaviour:

    * **2xx** — commit offset, continue.
    * **4xx** — send to DLQ, commit offset, log warning.
    * **5xx / network error** — retry with exponential backoff; offset is
      NOT committed until a successful POST (or max retries exhausted).
    * **max retries** — log error, leave offset uncommitted so Kafka
      re-delivers on the next poll.
    """

    def __init__(
        self,
        *,
        kafka_brokers: str,
        gateway_base_url: str,
        gateway_collector_token: str,
        kafka_topic: str = _DEFAULT_KAFKA_TOPIC,
        kafka_dlq_topic: str = _DEFAULT_KAFKA_DLQ_TOPIC,
        consumer_group_id: str = _DEFAULT_CONSUMER_GROUP_ID,
        max_retries: int = _MAX_RETRIES,
        initial_backoff: float = _INITIAL_BACKOFF_SECONDS,
        max_backoff: float = _MAX_BACKOFF_SECONDS,
    ) -> None:
        self._kafka_brokers = kafka_brokers
        self._gateway_base_url = gateway_base_url.rstrip("/")
        self._gateway_collector_token = gateway_collector_token
        self._kafka_topic = kafka_topic
        self._kafka_dlq_topic = kafka_dlq_topic
        self._consumer_group_id = consumer_group_id
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff

        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._in_flight: asyncio.Task[Any] | None = None

    # ── Factory ────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> Consumer:
        """Create a Consumer from environment variables.

        Required env vars:
            ``GATEWAY_KAFKA_BROKERS``, ``GATEWAY_BASE_URL``,
            ``GATEWAY_COLLECTOR_TOKEN``

        Optional env vars (fall back to defaults):
            ``GATEWAY_KAFKA_TOPIC``, ``GATEWAY_KAFKA_DLQ_TOPIC``,
            ``GATEWAY_CONSUMER_GROUP_ID``
        """
        kafka_brokers = os.getenv("GATEWAY_KAFKA_BROKERS", "")
        base_url = os.getenv("GATEWAY_BASE_URL", "")
        collector_token = os.getenv("GATEWAY_COLLECTOR_TOKEN", "")

        missing: list[str] = []
        if not kafka_brokers:
            missing.append("GATEWAY_KAFKA_BROKERS")
        if not base_url:
            missing.append("GATEWAY_BASE_URL")
        if not collector_token:
            missing.append("GATEWAY_COLLECTOR_TOKEN")

        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        return cls(
            kafka_brokers=kafka_brokers,
            gateway_base_url=base_url,
            gateway_collector_token=collector_token,
            kafka_topic=os.getenv("GATEWAY_KAFKA_TOPIC", _DEFAULT_KAFKA_TOPIC),
            kafka_dlq_topic=os.getenv("GATEWAY_KAFKA_DLQ_TOPIC", _DEFAULT_KAFKA_DLQ_TOPIC),
            consumer_group_id=os.getenv("GATEWAY_CONSUMER_GROUP_ID", _DEFAULT_CONSUMER_GROUP_ID),
        )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise Kafka consumer/producer and HTTP client, begin polling."""
        self._http_client = httpx.AsyncClient(
            base_url=self._gateway_base_url,
            headers={"Authorization": f"Bearer {self._gateway_collector_token}"},
        )
        self._consumer = AIOKafkaConsumer(
            self._kafka_topic,
            bootstrap_servers=self._kafka_brokers,
            group_id=self._consumer_group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=None,  # Deserialization is done in _process_message
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._kafka_brokers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )

        await self._consumer.start()
        await self._producer.start()
        self._running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._signal_handler)
            except NotImplementedError:
                # Signal handlers not available on this platform
                pass

        logger.info(
            "Consumer started: topic=%s group=%s",
            self._kafka_topic,
            self._consumer_group_id,
        )

    async def run(self) -> None:
        """Run the main poll loop.

        Blocks until :meth:`stop` is called or a fatal error occurs.
        Gracefully handles Kafka connection errors by re-creating the
        consumer after a brief back-off.

        Uses an explicit iterator with a 1-second timeout so that
        ``self._running`` is checked even when no messages arrive
        (SIGTERM/SIGINT can interrupt the loop cleanly).
        """
        if self._consumer is None or self._http_client is None:
            raise RuntimeError("Consumer not started — call start() first")

        while self._running:
            try:
                iterator = self._consumer.__aiter__()  # type: ignore[union-attr]
                while self._running:
                    try:
                        msg = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=1.0,
                        )
                    except TimeoutError:
                        continue  # re-check _running
                    except StopAsyncIteration:
                        break

                    # ── Process the message with exception safety ──────
                    try:
                        self._in_flight = asyncio.ensure_future(
                            self._process_message(msg)
                        )
                        await self._in_flight
                    except Exception:
                        logger.exception(
                            "Unhandled exception processing message "
                            "at offset %d — skipping",
                            msg.offset,
                        )
                    finally:
                        self._in_flight = None
            except KafkaError:
                if self._running:
                    logger.exception(
                        "Kafka error in poll loop — re-creating consumer"
                    )
                    await self._recreate_consumer()
            except asyncio.CancelledError:
                logger.info("Consumer poll loop cancelled")
                break

    async def stop(self) -> None:
        """Gracefully shut down the consumer.

        Stops polling, waits for any in-flight POST to complete, commits
        the latest offset, and closes all connections.
        """
        logger.info("Stopping consumer …")
        self._running = False
        self._shutdown_event.set()

        # Wait for in-flight POST to finish
        if self._in_flight is not None and not self._in_flight.done():
            try:
                await asyncio.wait_for(self._in_flight, timeout=30.0)
            except TimeoutError:
                logger.warning("In-flight POST timed out during shutdown")
                self._in_flight.cancel()

        if self._consumer:
            await self._consumer.stop()
        if self._producer:
            await self._producer.stop()
        if self._http_client:
            await self._http_client.aclose()

        logger.info("Consumer stopped")

    # ── Signal handling ────────────────────────────────────────────────

    def _signal_handler(self) -> None:
        """Handle SIGTERM / SIGINT — trigger graceful shutdown."""
        logger.info("Shutdown signal received — draining …")
        self._running = False
        self._shutdown_event.set()

    # ── Internal helpers ────────────────────────────────────────────────

    async def _recreate_consumer(self) -> None:
        """Stop and recreate the Kafka consumer after a connection error.

        Uses exponential backoff with jitter to avoid reconnect storms.
        """
        if self._consumer:
            await self._consumer.stop()

        import random

        delay = 1.0
        for attempt in range(10):
            try:
                self._consumer = AIOKafkaConsumer(
                    self._kafka_topic,
                    bootstrap_servers=self._kafka_brokers,
                    group_id=self._consumer_group_id,
                    enable_auto_commit=False,
                    auto_offset_reset="earliest",
                    value_deserializer=None,  # Deserialization is done in _process_message
                )
                await self._consumer.start()
                return
            except KafkaError:
                if attempt < 9:
                    jitter = random.uniform(0.5, 1.5)
                    await asyncio.sleep(delay * jitter)
                    delay = min(delay * 2, 60.0)
                    continue
                raise

    async def _process_message(self, msg: ConsumerRecord) -> None:
        """Process a single Kafka message — deserialize JSON, validate, POST, handle outcome."""
        # ── Deserialise JSON ────────────────────────────────────────
        try:
            raw_value = json.loads(msg.value.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            logger.warning(
                "Unparseable message (key=%s offset=%d) — sending to DLQ: %s",
                msg.key,
                msg.offset,
                exc,
            )
            await self._send_to_dlq(
                {"raw": msg.value.decode("utf-8", errors="replace")},
                reason=f"JSON decode failure: {exc}",
            )
            await self._commit()
            return

        # ── Validate the payload ────────────────────────────────────
        try:
            payload = IngestRequest.model_validate(raw_value)
        except Exception:
            logger.warning(
                "Invalid message shape — sending to DLQ (key=%s offset=%d)",
                msg.key,
                msg.offset,
            )
            await self._send_to_dlq(
                raw_value if isinstance(raw_value, dict) else {},
                reason="Invalid message shape — failed Pydantic validation",
            )
            await self._commit()
            return

        # ── POST to Gateway with retry ───────────────────────────
        request_json = payload.model_dump(mode="json")
        for attempt in range(self._max_retries):
            try:
                resp = await self._http_client.post(  # type: ignore[union-attr]
                    "/ingest", json=request_json
                )
            except httpx.RequestError:
                # Network / connection error — retryable
                if attempt < self._max_retries - 1:
                    delay = min(
                        self._initial_backoff * (2**attempt),
                        self._max_backoff,
                    )
                    logger.warning(
                        "Network error (attempt %d/%d) — retrying in %.1fs",
                        attempt + 1,
                        self._max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "Max retries (%d) exhausted after network errors — "
                    "offset not committed, Kafka will re-deliver",
                    self._max_retries,
                )
                return

            # ── 2xx — success ──────────────────────────────────
            if 200 <= resp.status_code < 300:
                await self._commit()
                return

            # ── 4xx — bad data, send to DLQ then commit ────────
            if 400 <= resp.status_code < 500:
                logger.warning(
                    "4xx response (%d) — sending to DLQ: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                await self._send_to_dlq(
                    request_json,
                    reason=f"HTTP {resp.status_code}",
                )
                await self._commit()
                return

            # ── 5xx — retry with backoff ───────────────────────
            if attempt < self._max_retries - 1:
                delay = min(
                    self._initial_backoff * (2**attempt),
                    self._max_backoff,
                )
                logger.warning(
                    "5xx response (%d) (attempt %d/%d) — retrying in %.1fs",
                    resp.status_code,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            logger.error(
                "Max retries (%d) exhausted after 5xx — "
                "offset not committed, Kafka will re-deliver",
                self._max_retries,
            )
            return

    async def _commit(self) -> None:
        """Commit the current Kafka offset."""
        if self._consumer:
            await self._consumer.commit()

    async def _send_to_dlq(self, payload: dict[str, Any], *, reason: str) -> None:
        """Send a message to the DLQ topic with context about the failure."""
        if self._producer is None:
            return
        dlq_payload: dict[str, Any] = {
            "original_topic": self._kafka_topic,
            "reason": reason,
            "payload": payload,
        }
        try:
            await self._producer.send_and_wait(self._kafka_dlq_topic, dlq_payload)
        except KafkaError:
            logger.exception("Failed to send message to DLQ topic %s", self._kafka_dlq_topic)


# ── Entry point ───────────────────────────────────────────────────────────


async def _main() -> None:
    """Entry point for the consumer when run as a standalone process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    consumer = Consumer.from_env()
    await consumer.start()
    try:
        await consumer.run()
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(_main())
