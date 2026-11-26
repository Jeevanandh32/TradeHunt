#!/bin/bash
# Build the host image and deploy the TradeHunt lab on the VM, then wait until multicast routing is ready.
# Fails with a message instead of waiting forever if a switch never boots or routing never comes up.
set -euo pipefail
cd "$(dirname "$0")"
LOG=/tmp/tradehunt-deploy.log

sudo docker build -q -t tradehunt-host:latest host/ >/dev/null
# Start clean: containers left stopped by an earlier VM shutdown can block a --reconfigure deploy.
sudo containerlab destroy -t tradehunt.clab.yml --cleanup >/dev/null 2>&1 || true
if ! sudo containerlab deploy -t tradehunt.clab.yml --reconfigure >"$LOG" 2>&1; then
  echo "containerlab deploy failed:"; tail -20 "$LOG"; exit 1
fi
grep -q "ERROR" "$LOG" && { echo "containerlab reported errors (lab may still work, checking):"; grep -A2 ERROR "$LOG" | head -6; }

for r in a-edge a-leaf b-edge b-leaf; do
  for i in $(seq 1 60); do  # up to 5 minutes per switch
    sudo docker exec clab-tradehunt-$r Cli -p 15 -c "show version" >/dev/null 2>&1 && continue 2
    sleep 5
  done
  echo "$r did not boot: $(sudo docker ps -a --filter name=clab-tradehunt-$r --format '{{.Status}}')"; exit 1
done
echo "switches booted"

# leaves are ready once they see their edge as a PIM neighbour and have an OSPF route to the source subnet
for side in a b; do
  n=$([ $side = a ] && echo 1 || echo 2)
  for i in $(seq 1 40); do
    if sudo docker exec clab-tradehunt-$side-leaf Cli -p 15 -c "show ip pim neighbor" | grep -q "10.$n.255.0" && \
       sudo docker exec clab-tradehunt-$side-leaf Cli -p 15 -c "show ip route 10.$n.0.0/24" | grep -qE "^ *O "; then
      echo "$side side: PIM neighbour and OSPF route to the feed $side source are up"
      continue 2
    fi
    sleep 5
  done
  echo "$side side: no PIM neighbour or OSPF route after 200 s"; exit 1
done
