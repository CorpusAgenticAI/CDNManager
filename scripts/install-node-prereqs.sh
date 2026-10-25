#!/usr/bin/env bash
# Ubuntu node prerequisites from the telemetry contract, section 16.
# Idempotent package install. OpenResty itself is installed by the node
# bootstrap, not by this list, because stock nginx must stay absent.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-venv python3-psutil python3-requests \
  smartmontools nvme-cli ethtool dmidecode pciutils \
  chrony xfsprogs rsync acl python3-apt rsyslog \
  libmaxminddb0 mmdb-bin geoipupdate \
  openssh-server sudo logrotate ca-certificates curl jq
install -m 0644 "$(dirname "$0")/60-cdn-node.conf" /etc/sysctl.d/60-cdn-node.conf
sysctl --system >/dev/null
echo "Node prerequisites installed. OpenResty and the agent unit are still required."
