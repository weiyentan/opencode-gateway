"""Run the JS frontend tests via pytest."""
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_DIR / "frontend"

JS_TESTS = [
    "test_pure_functions.js",
    "test_change_request_list.js",
]


@pytest.mark.parametrize("test_file", JS_TESTS, ids=lambda p: Path(p).name)
def test_js_frontend(test_file):
    """Run a single JS frontend test via node."""
    script = FRONTEND_DIR / "tests" / test_file
    assert script.exists(), f"Test file missing: {script}"
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(FRONTEND_DIR.parent),
    )
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    assert result.returncode == 0, (
        f"JS test {test_file} failed with exit code {result.returncode}\n"
        f"stdout: {result.stdout[-2000:]}\n"
        f"stderr: {result.stderr[-2000:]}"
    )
