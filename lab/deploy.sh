#!/bin/bash
# Build the host image and deploy the TradeHunt lab on the VM, then wait until multicast routing is ready.
set -euo pipefail
cd "$(dirname "$0")"

sudo docker build -q -t tradehunt-host:latest host/ >/dev/null
sudo containerlab deploy -t tradehunt.clab.yml --reconfigure >/dev/null

for r in a-edge a-leaf b-edge b-leaf; do
  until sudo docker exec clab-tradehunt-$r Cli -p 15 -c "show version" >/dev/null 2>&1; do sleep 5; done
done
echo "switches booted"

# leaves are ready once they see their edge as a PIM neighbour and have an OSPF route to the source subnet
for side in a b; do
  n=$([ $side = a ] && echo 1 || echo 2)
  for i in $(seq 1 40); do
    sudo docker exec clab-tradehunt-$side-leaf Cli -p 15 -c "show ip pim neighbor" | grep -q "10.$n.255.0" && \
    sudo docker exec clab-tradehunt-$side-leaf Cli -p 15 -c "show ip route 10.$n.0.0/24" | grep -qE "^ *O " && break
    sleep 5
  done
  echo "$side side: PIM neighbour and OSPF route to the feed $side source are up"
done
