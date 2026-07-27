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

    try:
        consumer = Consumer.from_env()
    except ValueError as exc:
        logger.error("Consumer configuration error: %s", exc)
        raise SystemExit(1) from exc

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
