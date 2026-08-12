"""Integration tests — require a real PostgreSQL database.

Start the test database before running:

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v
"""
