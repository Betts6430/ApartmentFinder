#!/usr/bin/env bash
# Run the unit tests. PYTEST_DISABLE_PLUGIN_AUTOLOAD isolates us from unrelated
# system pytest plugins (e.g. ROS's launch_testing) that leak in via the global
# site-packages and fail to import. Pass extra pytest args through: ./run_tests.sh -k phone
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest "$@"
