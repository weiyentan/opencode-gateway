#!/usr/bin/env python3
"""Run the integration test directly."""
import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
venv_python = os.path.join(".venv", "bin", "python")

result = subprocess.run(
    [venv_python, "-m", "pytest",
     "tests/integration/test_afk_lifecycle_multi_stage.py",
     "--collect-only", "-q"],
    capture_output=True, text=True
)
print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)
print("RC:", result.returncode)
