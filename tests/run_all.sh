#!/bin/sh
# Offline test suite: no camera, no network beyond loopback. Run from anywhere.
cd "$(dirname "$0")/.." || exit 1
if [ -z "$PY" ]; then
  PY=.venv/bin/python
  # in a git worktree there is no .venv: use the main checkout's
  [ -x "$PY" ] || PY="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)/.venv/bin/python"
fi
set -e
# tests run against a throwaway data home seeded from the example camera table (never the user's real data)
KINORA_HOME="$(mktemp -d)"; export KINORA_HOME
trap 'rm -rf "$KINORA_HOME"' EXIT
cp cameras.example.json "$KINORA_HOME/cameras.json"
export KINORA_NO_INSTANCE_LOCK=1
$PY -m unittest tests.test_gate tests.test_listings_export tests.test_slots tests.test_slots_register tests.test_session_lock tests.test_errors tests.test_live_argv tests.test_config_home \
  tests.test_gate_hardening tests.test_instance tests.test_live_gate \
  tests.test_creds tests.test_creds_percam tests.test_gate_recent_fails tests.test_binaries tests.test_health_ping tests.test_hls_orphans tests.test_live_sweep \
  tests.test_p2p_punch tests.test_pytapo_guard tests.test_registry tests.test_setup_api tests.test_discover tests.test_thumb tests.test_native_thumb tests.test_cli
$PY tests/test_udt.py
$PY tests/test_p2p_setup.py
$PY tests/test_play.py
command -v node >/dev/null && node tests/test_prefetch.js
command -v node >/dev/null && node tests/test_setup_logic.js
command -v node >/dev/null && node tests/test_player_logic.js
