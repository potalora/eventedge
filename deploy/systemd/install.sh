#!/usr/bin/env bash
# Install/refresh the EventEdge systemd units on the VPS. Idempotent.
# Usage: sudo ./install.sh   (run from this directory on the VPS)
set -euo pipefail

UNIT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="/home/hermes/trading_agents"

mkdir -p "$REPO_ROOT/.triggers"
chown hermes:hermes "$REPO_ROOT/.triggers"

for unit in trade.service trade.timer trade-rerun.service trade-rerun.path trade-preflight.service trade-preflight.timer; do
    cp "$UNIT_DIR/$unit" /etc/systemd/system/
done

systemctl daemon-reload
systemctl enable --now trade.timer trade-rerun.path trade-preflight.timer

echo "--- installed:"
systemctl list-timers trade.timer trade-preflight.timer --no-pager
systemctl status trade-rerun.path --no-pager | head -5
