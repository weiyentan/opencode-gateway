"""Kafka consumer that reads usage records from topic ``opencode-usage``
and POSTs them to the Gateway's ``/ingest`` endpoint."""

from app.consumer.consumer import KafkaConsumer

__all__ = ["KafkaConsumer"]
