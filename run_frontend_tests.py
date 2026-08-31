"""Run the JS frontend tests via subprocess."""
import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

tests = [
    'frontend/tests/test_pure_functions.js',
    'frontend/tests/issue_577_tests.js',
    'frontend/tests/test_change_request_list.js',
]

all_pass = True
for t in tests:
    print(f'\n{"="*60}')
    print(f'  Running: {t}')
    print(f'{"="*60}')
    result = subprocess.run(['node', t], capture_output=True, text=True, timeout=60)
    print(result.stdout)
    if result.stderr:
        print('STDERR:', result.stderr)
    if result.returncode != 0:
        all_pass = False
        print(f'  FAILED with exit code {result.returncode}')

print(f'\n{"="*60}')
if all_pass:
    print('  ALL JS TESTS PASSED')
else:
    print('  SOME JS TESTS FAILED')
print(f'{"="*60}')
sys.exit(0 if all_pass else 1)
