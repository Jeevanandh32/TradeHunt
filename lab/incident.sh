#!/bin/bash
# Inject or fix a TradeHunt incident.
#   ./incident.sh inject <n>    ./incident.sh fix <n>    ./incident.sh list
#
#  1  publisher multicast TTL = 1                    (exchange)
#  2  Linux rp_filter drops feed B on sub1           (sub1)
#  3  no IGMP querier/router on VLAN 110             (a-leaf)
#  4  RPF broken by a leftover blackhole route       (a-leaf)
#  5  socket receive buffer too small on sub1        (sub1)
#  6  feed A silently dead: a-edge uplink shut down  (a-edge)
set -uo pipefail
cd "$(dirname "$0")"
ACTION=${1:-list}; N=${2:-0}
X="sudo docker exec"
eos() { $X clab-tradehunt-$1 Cli -p 15 -c "$(printf "configure\n%s\nend" "$2")" >/dev/null; }

# restart sub1's feed handler with extra options (incident 5)
restart_sub1_handler() {
  $X clab-tradehunt-sub1 sh -c 'pkill -f handler.py' 2>/dev/null; sleep 1
  $X -d -w /opt/feed clab-tradehunt-sub1 sh -c "python3 handler.py --name sub1 --local-a 10.1.10.11 --local-b 10.2.10.11 $1 > /tmp/handler.log 2>&1"
}

case "$ACTION:$N" in
  list:*)
    sed -n '5,10p' "$0" | sed 's/^# //' ;;

  inject:1) PUBEXTRA="--ttl 1" bash feeds.sh start >/dev/null ;;
  fix:1)    bash feeds.sh start >/dev/null ;;

  inject:2)
    # no route back to the B source via eth2, plus strict reverse-path filtering
    $X clab-tradehunt-sub1 sh -c 'ip route del 10.2.0.0/16 via 10.2.10.1 dev eth2; sysctl -qw net.ipv4.conf.all.rp_filter=1 net.ipv4.conf.eth2.rp_filter=1' ;;
  fix:2)
    $X clab-tradehunt-sub1 sh -c 'ip route replace 10.2.0.0/16 via 10.2.10.1 dev eth2; sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.eth2.rp_filter=0' ;;

  inject:3) eos a-leaf "interface Vlan110
no pim ipv4 sparse-mode" ;;
  fix:3)    eos a-leaf "interface Vlan110
pim ipv4 sparse-mode" ;;

  inject:4) eos a-leaf "ip route 10.1.0.0/24 Null0" ;;
  fix:4)    eos a-leaf "no ip route 10.1.0.0/24 Null0" ;;

  # the kernel rounds tiny buffers up to its minimum: room for only a couple of packets
  inject:5) restart_sub1_handler "--rcvbuf 2048" ;;
  fix:5)    restart_sub1_handler "" ;;

  inject:6) eos a-edge "interface Ethernet2
shutdown" ;;
  fix:6)    eos a-edge "interface Ethernet2
no shutdown" ;;

  *) echo "usage: $0 inject|fix <1-6> | list"; exit 1 ;;
esac
