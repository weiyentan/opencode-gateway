"""Production entry point — provides the ``app`` instance for ``uvicorn app.main:app``."""

from app.core.factory import create_app

app = create_app()
