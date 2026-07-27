"""Entry point — ``python -m app.consumer`` starts the Kafka consumer."""

from __future__ import annotations

import asyncio
import logging

from app.consumer.consumer import Consumer

logger = logging.getLogger(__name__)


def main() -> None:
    """Create a :class:`Consumer`, and run until shutdown."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    consumer = Consumer.from_env()

    if not consumer._kafka_brokers:
        logger.error("GATEWAY_KAFKA_BROKERS is required but not set.")
        raise SystemExit(1)

    if not consumer._gateway_base_url:
        logger.error("GATEWAY_BASE_URL is required but not set.")
        raise SystemExit(1)

    async def _run() -> None:
        await consumer.start()
        try:
            await consumer.run()
        finally:
            await consumer.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
