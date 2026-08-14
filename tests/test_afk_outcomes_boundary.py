"""Mechanical boundary test: the ``afk_outcomes`` package is pure domain.

Enforces, mechanically, that ``afk_outcomes`` never imports an application
module (``app`` or ``app.*``):

1. A static scan of every ``.py`` file in the package for ``import app`` /
   ``from app`` statements.
2. A fresh subprocess import that proves importing the package pulls in no
   application modules (important because the test-suite ``conftest``
   already imports ``app`` into the parent process).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AFK_PACKAGE = REPO_ROOT / "afk_outcomes"


def _source_files() -> list[Path]:
    return sorted(AFK_PACKAGE.rglob("*.py"))


def test_package_exists_and_is_a_top_level_sibling_of_app() -> None:
    assert AFK_PACKAGE.is_dir(), "afk_outcomes package is missing"
    assert (AFK_PACKAGE / "__init__.py").is_file(), "afk_outcomes/__init__.py is missing"
    assert (REPO_ROOT / "app").is_dir(), "app package is missing (unexpected layout)"
    # No src-layout: the package must sit at the repository top level.
    assert not (REPO_ROOT / "src").exists(), "unexpected src-layout directory"


def test_no_application_imports_in_source() -> None:
    offenders: list[str] = []
    for path in _source_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("import app") or stripped.startswith("from app"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, (
        "afk_outcomes must not import application modules; found:\n"
        + "\n".join(offenders)
    )


def test_no_application_string_references_in_source() -> None:
    """Guard against sneaky dynamic imports (``importlib.import_module('app...')``)."""
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for token in ("import_module", "__import__"):
            for lineno, line in enumerate(text.splitlines(), start=1):
                if token in line and "app" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "afk_outcomes must not reference application modules; found:\n"
        + "\n".join(offenders)
    )


def test_importing_package_does_not_import_app_modules() -> None:
    code = (
        "import sys\n"
        "import afk_outcomes as pkg\n"
        "print('IMPORTED=' + getattr(pkg, '__name__', '?'))\n"
        "for name in sorted(sys.modules):\n"
        "    if name == 'app' or name.startswith('app.'):\n"
        "        print('APP_MODULE=' + name)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"subprocess import failed: {result.stderr}"
    assert "IMPORTED=afk_outcomes" in result.stdout, result.stdout
    app_modules = [line for line in result.stdout.splitlines() if line.startswith("APP_MODULE=")]
    assert not app_modules, (
        "importing afk_outcomes pulled in application modules: " + repr(app_modules)
    )


def test_public_api_exposes_expected_symbols() -> None:
    import afk_outcomes

    expected = {
        "AFKRun",
        "Correlation",
        "CorrelationEvidence",
        "CorrelationRule",
        "EngineeringEntity",
        "EngineeringEvent",
        "EngineeringOutcome",
        "EngineeringOutcomeStatus",
        "EntityType",
        "OutcomeRepository",
        "Provider",
        "ProviderAdapter",
        "RunEntityLink",
        "RunSessionLink",
        "RunStatus",
        "ULIDSource",
        "dumps_canonical",
        "loads_canonical",
    }
    missing = expected - set(afk_outcomes.__all__)
    assert not missing, f"public API is missing: {sorted(missing)}"
