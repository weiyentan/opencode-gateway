"""Production entry point — provides the ``app`` instance for ``uvicorn app.main:app``.

The Gateway is now an OpenCode observability service.
"""

from app.core.factory import create_app

app = create_app()
