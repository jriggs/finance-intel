#!/usr/bin/env bash
# Run the test suite from anywhere in the project.
# Usage:
#   ./run_tests.sh           — run all tests
#   ./run_tests.sh -v        — verbose output
#   ./run_tests.sh -k search — run only tests matching "search"

set -e
cd "$(dirname "$0")/backend"
source venv/bin/activate

# Install pytest if not already present
pip install pytest --quiet

exec pytest tests/ "$@"
