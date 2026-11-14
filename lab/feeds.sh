#!/bin/bash
# Start, stop or check the market-data programs inside the lab.
#   ./feeds.sh start [speed] [from] [to]   retransmitter + a handler on each server + publisher
#   ./feeds.sh stop
#   ./feeds.sh status
#
# Optional environment:
#   PUBLOOP=""          replay the window once instead of looping (used by feed-test.sh)
#   PUBEXTRA="--ttl 1"  extra publisher options (used by incident 1)
#   HANDLER_ARGS="..."  extra handler options, e.g. "--rcvbuf 65536"
set -uo pipefail
ACTION=${1:-status}; SPEED=${2:-0.1}; FROM=${3:-09:29:30}; TO=${4:-09:31:00}
DATA=/data/itch50_05_15_open_slice.bin
X="sudo docker exec"

stop_all() {
  $X clab-tradehunt-exchange sh -c 'pkill -f "publisher.py|retransmitter.py"; rm -f /tmp/publisher-stats.json' 2>/dev/null
  for s in sub1 sub2; do $X clab-tradehunt-$s sh -c 'pkill -f handler.py; rm -f /tmp/handler-stats.json' 2>/dev/null; done
  sleep 1
}

case $ACTION in
  start)
    stop_all
    $X -d -w /opt/feed clab-tradehunt-exchange sh -c "python3 retransmitter.py $DATA --from $FROM --to $TO > /tmp/retransmitter.log 2>&1"
    for s in sub1 sub2; do
      n=$([ $s = sub1 ] && echo 11 || echo 12)
      $X -d -w /opt/feed clab-tradehunt-$s sh -c "python3 handler.py --name $s --local-a 10.1.10.$n --local-b 10.2.10.$n ${HANDLER_ARGS:-} > /tmp/handler.log 2>&1"
    done
    sleep 8  # the retransmitter needs a few seconds to load the data
    $X -d -w /opt/feed clab-tradehunt-exchange sh -c "python3 publisher.py $DATA --speed $SPEED --from $FROM --to $TO ${PUBLOOP---loop} ${PUBEXTRA:-} > /tmp/publisher.log 2>&1"
    echo "feeds started: speed $SPEED, window $FROM-$TO ${PUBLOOP---loop}"
    ;;
  stop)
    stop_all
    echo "feeds stopped"
    ;;
  status)
    for c in exchange sub1 sub2; do
      echo "$c: $($X clab-tradehunt-$c sh -c 'ps -eo args | grep -E "^python3 (publisher|retransmitter|handler).py" | cut -c1-60')"
    done
    ;;
esac
