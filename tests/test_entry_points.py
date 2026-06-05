"""Tests for the production and dev entry points."""

from fastapi import FastAPI


def test_main_module_exports_app():
    """app/main.py must export a FastAPI 'app' instance for uvicorn."""
    from app.main import app

    assert isinstance(app, FastAPI)


def test_main_title_matches():
    """The app exported by main should have the correct title."""
    from app.main import app

    assert app.title == "OpenCode Gateway"
