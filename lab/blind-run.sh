#!/bin/bash
# Blind runs: pick a random incident (1-6), inject it without saying which, let tradehunt diagnose it,
# then score the diagnosis against what was injected.   ./blind-run.sh [runs]
set -uo pipefail
cd "$(dirname "$0")"
RUNS=${1:-8}
declare -A EXPECT=([1]=ttl [2]=rp_filter [3]=igmp_querier [4]=rpf [5]=rcvbuf [6]=link_down)
OUT=~/tradehunt/blind-runs.tsv
echo -e "run\tinjected\texpected\tdiagnosed\tcorrect\tseconds_to_diagnose" > $OUT
correct=0
for r in $(seq 1 $RUNS); do
  n=$(( (RANDOM % 6) + 1 ))
  bash incident.sh inject $n
  sleep 20   # let the symptom develop (the handler's 10 s rate window must go quiet)
  res=$(sudo python3 ../tools/tradehunt.py diagnose --json)
  got=$(echo "$res" | python3 -c 'import json,sys; print(json.load(sys.stdin)["root_cause"])')
  secs=$(echo "$res" | python3 -c 'import json,sys; print(json.load(sys.stdin)["seconds"])')
  ok=no; [ "$got" = "${EXPECT[$n]}" ] && { ok=yes; correct=$((correct+1)); }
  echo -e "$r\t$n\t${EXPECT[$n]}\t$got\t$ok\t$secs" >> $OUT
  bash incident.sh fix $n
  sleep 40   # recover before the next run (a restored link takes ~12 s, then the 20 s gap window must clear)
done
echo "correct: $correct of $RUNS"
column -t $OUT
