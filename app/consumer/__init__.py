"""Kafka consumer bridge.

Reads usage records from Kafka and POSTs them to the Gateway ingest API.
"""

from app.consumer.consumer import Consumer

__all__ = ["Consumer"]
