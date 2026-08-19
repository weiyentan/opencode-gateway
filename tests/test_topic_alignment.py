"""Deployment-topic alignment tests for the AFK outcome consumer (issue #531).

The AFK outcome consumer subscribes to ``settings.normalized_events_topic``
(``engineering.events.normalized``) and DLQs to
``settings.normalized_events_dlq_topic``.  The deployment manifests used to
configure the legacy ``GATEWAY_AFK_OUTCOMES_TOPIC=afk.events`` /
``GATEWAY_AFK_OUTCOMES_DLQ_TOPIC=afk.events-dlq`` variables instead — topics
the consumer no longer reads — so the deployed configuration silently
diverged from the consumer's actual subscription.

These tests pin the configured deployment topic to the consumer's
subscription topic so the two can never silently diverge again:

* both manifests must configure ``GATEWAY_NORMALIZED_EVENTS_TOPIC`` /
  ``GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC``;
* the configured values must equal the consumer's subscription settings;
* ``AFKOutcomeConsumer.from_env()`` fed those exact values must subscribe
  to them;
* the legacy topic variables must no longer be configured (they remain
  compatibility-only settings in ``app/core/config.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"
K8S_DEPLOYMENT = REPO_ROOT / "k8s" / "afk-consumer-deployment.yaml"

AFK_CONSUMER_SERVICE = "afk-outcomes-consumer"
CONSUMER_CONTAINER = "opencode-afk-outcomes-consumer"

NORMALIZED_TOPIC = "engineering.events.normalized"
NORMALIZED_DLQ_TOPIC = "engineering.events.normalized.dlq"


def _compose_env() -> dict[str, str]:
    """The ``environment`` map of the compose AFK consumer service."""
    import yaml

    with open(COMPOSE_FILE, encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    service_env = compose["services"][AFK_CONSUMER_SERVICE]["environment"]
    return {str(k): str(v) for k, v in service_env.items()}


def _compose_default(value: str) -> str:
    """Resolve a compose ``${VAR:-default}`` interpolation to its default."""
    if "${" in value and ":-" in value:
        return value.split(":-", 1)[1].rstrip("}")
    return value


def _k8s_env() -> dict[str, str]:
    """The literal (``value``-keyed) env entries of the k8s consumer container."""
    import yaml

    with open(K8S_DEPLOYMENT, encoding="utf-8") as f:
        deployment = yaml.safe_load(f)
    containers = deployment["spec"]["template"]["spec"]["containers"]
    container = next(c for c in containers if c["name"] == CONSUMER_CONTAINER)
    return {
        entry["name"]: str(entry["value"])
        for entry in container["env"]
        if "value" in entry
    }


@pytest.fixture(autouse=True)
def _deployment_envs() -> dict[str, dict[str, str]]:
    return {"compose": _compose_env(), "k8s": _k8s_env()}


class TestDeploymentTopicConfiguration:
    """Both deployment manifests configure the normalized-events topics."""

    def test_compose_configures_normalized_events_topics(self, _deployment_envs) -> None:
        env = _deployment_envs["compose"]
        assert env["GATEWAY_NORMALIZED_EVENTS_TOPIC"] is not None
        assert _compose_default(env["GATEWAY_NORMALIZED_EVENTS_TOPIC"]) == NORMALIZED_TOPIC
        assert (
            _compose_default(env["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"])
            == NORMALIZED_DLQ_TOPIC
        )

    def test_k8s_configures_normalized_events_topics(self, _deployment_envs) -> None:
        env = _deployment_envs["k8s"]
        assert env["GATEWAY_NORMALIZED_EVENTS_TOPIC"] == NORMALIZED_TOPIC
        assert env["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"] == NORMALIZED_DLQ_TOPIC

    def test_legacy_topic_variables_are_not_configured(self, _deployment_envs) -> None:
        """The legacy variables are removed from the manifests (compatibility-only)."""
        for env in _deployment_envs.values():
            assert "GATEWAY_AFK_OUTCOMES_TOPIC" not in env, (
                "Legacy GATEWAY_AFK_OUTCOMES_TOPIC must not be configured — the "
                "consumer does not subscribe to it (it is a compatibility-only "
                "Settings field)."
            )
            assert "GATEWAY_AFK_OUTCOMES_DLQ_TOPIC" not in env, (
                "Legacy GATEWAY_AFK_OUTCOMES_DLQ_TOPIC must not be configured — the "
                "consumer does not read it (it is a compatibility-only Settings field)."
            )

    def test_deployment_topics_match_across_manifests(self, _deployment_envs) -> None:
        compose_topic = _compose_default(
            _deployment_envs["compose"]["GATEWAY_NORMALIZED_EVENTS_TOPIC"]
        )
        k8s_topic = _deployment_envs["k8s"]["GATEWAY_NORMALIZED_EVENTS_TOPIC"]
        assert compose_topic == k8s_topic, (
            "Compose and Kubernetes must configure the same consumer topic."
        )
        compose_dlq = _compose_default(
            _deployment_envs["compose"]["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"]
        )
        k8s_dlq = _deployment_envs["k8s"]["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"]
        assert compose_dlq == k8s_dlq, (
            "Compose and Kubernetes must configure the same consumer DLQ topic."
        )


class TestDeploymentTopicEqualsConsumerSubscription:
    """The configured deployment topic equals the consumer's subscription topic."""

    def test_configured_topic_equals_settings_defaults(self, _deployment_envs, monkeypatch) -> None:
        from app.core.config import Settings

        # Isolate from ambient topic overrides so the assertion compares the
        # manifests against the *defaults* the consumer falls back on.
        monkeypatch.delenv("GATEWAY_NORMALIZED_EVENTS_TOPIC", raising=False)
        monkeypatch.delenv("GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC", raising=False)

        settings = Settings(
            _env_file=None,
            api_key="test-key-for-topic-alignment",  # production-mode validator
        )
        compose_topic = _compose_default(
            _deployment_envs["compose"]["GATEWAY_NORMALIZED_EVENTS_TOPIC"]
        )
        k8s_topic = _deployment_envs["k8s"]["GATEWAY_NORMALIZED_EVENTS_TOPIC"]
        compose_dlq = _compose_default(
            _deployment_envs["compose"]["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"]
        )
        k8s_dlq = _deployment_envs["k8s"]["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"]

        assert compose_topic == k8s_topic == settings.normalized_events_topic, (
            "The deployment topic must equal the topic the consumer reads "
            "(settings.normalized_events_topic)."
        )
        assert (
            compose_dlq == k8s_dlq == settings.normalized_events_dlq_topic
        ), (
            "The deployment DLQ topic must equal the DLQ topic the consumer writes "
            "(settings.normalized_events_dlq_topic)."
        )

    @pytest.mark.asyncio
    async def test_from_env_subscribes_to_configured_topic(self, _deployment_envs) -> None:
        """``AFKOutcomeConsumer.from_env()`` wires the configured topic into the
        consumer's Kafka subscription (``consumer._topic`` / ``_dlq_topic``)."""
        compose_topic = _compose_default(
            _deployment_envs["compose"]["GATEWAY_NORMALIZED_EVENTS_TOPIC"]
        )
        compose_dlq = _compose_default(
            _deployment_envs["compose"]["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"]
        )
        assert compose_topic == _deployment_envs["k8s"]["GATEWAY_NORMALIZED_EVENTS_TOPIC"]
        assert (
            compose_dlq
            == _deployment_envs["k8s"]["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"]
        )

        env_vars = {
            "GATEWAY_ENV": "development",
            "GATEWAY_KAFKA_BROKERS": "broker1:9092",
            "GATEWAY_NORMALIZED_EVENTS_TOPIC": compose_topic,
            "GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC": compose_dlq,
            "GATEWAY_AFK_OUTCOMES_CONSUMER_GROUP_ID": "opencode-outcomes",
            "GATEWAY_AFK_OUTCOMES_PROVIDER": "github",
            "GATEWAY_AFK_OUTCOMES_REPOSITORY": "owner/repo",
        }
        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("app.consumer.afk_consumer.asyncpg.create_pool", new_callable=AsyncMock),
            patch(
                "app.consumer.afk_consumer._build_adapter",
                return_value=(None, None),
            ),
        ):
            from app.consumer.afk_consumer import AFKOutcomeConsumer

            consumer = await AFKOutcomeConsumer.from_env()

        assert consumer._topic == compose_topic == NORMALIZED_TOPIC, (
            "The consumer must subscribe to the deployment-configured topic."
        )
        assert consumer._dlq_topic == compose_dlq == NORMALIZED_DLQ_TOPIC, (
            "The consumer must DLQ to the deployment-configured DLQ topic."
        )
