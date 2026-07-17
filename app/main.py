"""Production entry point — provides the ``app`` instance for ``uvicorn app.main:app``.

The Gateway is now an OpenCode observability service.
"""

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.core.factory import create_app

app = create_app()

# Mount the Aurora Glass frontend at the root.  API routes registered
# inside the factory are matched first; the static mount captures
# everything else (index.html, CSS, JS).
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(_frontend_dir), html=True),
        name="frontend",
    )
