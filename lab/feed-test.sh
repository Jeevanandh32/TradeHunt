#!/bin/bash
# Replay real data once and check what both servers received, with or without injected loss.
#   ./feed-test.sh                       all three cases
#   CASES="3" ./feed-test.sh             just one case
#   FROM=09:29:58 TO=09:30:05 ./feed-test.sh   a different exchange-time window
#
# Cases: 1 clean, 2 2% loss on feed A only (arbitration should cover it),
#        3 2% loss on both feeds (retransmission should recover what both lost)
set -uo pipefail
cd "$(dirname "$0")"
FROM=${FROM:-09:29:58}; TO=${TO:-09:30:01}; SPEED=${SPEED:-0.1}; CASES=${CASES:-1 2 3}
X="sudo docker exec"

clear_loss() { for i in eth1 eth2; do $X clab-tradehunt-exchange tc qdisc del dev $i root 2>/dev/null; done; }
add_loss()   { $X clab-tradehunt-exchange tc qdisc add dev $1 root netem loss 2%; }

run_case() {
  PUBLOOP="" bash feeds.sh start $SPEED $FROM $TO >/dev/null
  sleep 5
  # wait for the single replay to finish, then give the handlers time to close any last gaps
  until ! $X clab-tradehunt-exchange pgrep -f publisher.py >/dev/null; do sleep 2; done
  sleep 4
  echo "=== $1"
  $X -w /opt/feed clab-tradehunt-exchange python3 report.py publisher /tmp/publisher-stats.json
  for s in sub1 sub2; do $X -w /opt/feed clab-tradehunt-$s python3 report.py handler /tmp/handler-stats.json $s; done
  bash feeds.sh stop >/dev/null
}

clear_loss
[[ " $CASES " == *" 1 "* ]] && run_case "1. clean"
[[ " $CASES " == *" 2 "* ]] && { add_loss eth1; run_case "2. 2% loss on feed A only"; clear_loss; }
[[ " $CASES " == *" 3 "* ]] && { add_loss eth1; add_loss eth2; run_case "3. 2% loss on BOTH feeds"; clear_loss; }
exit 0
