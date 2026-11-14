#!/usr/bin/env bash
# Run once on a fresh Ubuntu/Debian VPS as root:  bash setup-server.sh
set -euo pipefail
apt-get update
apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
mkdir -p /opt && cd /opt
if [ ! -d openjarvis ]; then
  git clone https://github.com/sebastianconcon31-hue/openjarvis.git
fi
cd openjarvis/callbot
[ -f .env ] || cp .env.example .env
[ -f salon.yaml ] || cp salon.example.yaml salon.yaml
[ -f google-credentials.json ] || echo '{}' > google-credentials.json
mkdir -p data
echo
echo "Now edit .env and salon.yaml (nano .env, nano salon.yaml), then run:"
echo "  cd /opt/openjarvis/callbot && docker compose up -d --build"
