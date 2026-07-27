"""Entry point — ``python -m app.consumer`` starts the Kafka consumer."""

from __future__ import annotations

import asyncio
import logging

from app.consumer.consumer import KafkaConsumer
from app.core.config import Settings

logger = logging.getLogger(__name__)


def main() -> None:
    """Parse settings, create a :class:`KafkaConsumer`, and run until shutdown."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = Settings()

    if not settings.kafka_brokers:
        logger.error("GATEWAY_KAFKA_BROKERS is required but not set.")
        raise SystemExit(1)

    if not settings.base_url:
        logger.error("GATEWAY_BASE_URL is required but not set.")
        raise SystemExit(1)

    consumer = KafkaConsumer(settings)

    try:
        asyncio.run(consumer.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
