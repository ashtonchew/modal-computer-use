#!/usr/bin/env bash
set -euo pipefail
export COMPUTER_USE_BACKEND="${COMPUTER_USE_BACKEND:-mock}"
export COMPUTER_USE_LOCAL_TOKEN="${COMPUTER_USE_LOCAL_TOKEN:-dev}"
python -m modal_computer_use.daemon
