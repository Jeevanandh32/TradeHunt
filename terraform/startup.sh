#!/bin/bash
# First-boot setup for the TradeHunt lab VM. Safe to re-run: each step checks before installing.
set -euo pipefail

# Package installs must never wait for an answer: there is no one at the console during first boot.
export DEBIAN_FRONTEND=noninteractive
echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

if ! command -v containerlab >/dev/null; then
  bash -c "$(curl -sL https://get.containerlab.dev)"
fi

apt-get update -y
apt-get install -y tcpdump tshark python3-venv python3-pip jq

# Larger socket buffers on the host; the buffer-overflow incident lowers these inside a container.
cat >/etc/sysctl.d/90-tradehunt.conf <<'EOF'
net.core.rmem_max = 67108864
net.core.rmem_default = 4194304
EOF
sysctl --system >/dev/null

mkdir -p /opt/tradehunt/images /opt/tradehunt/data
