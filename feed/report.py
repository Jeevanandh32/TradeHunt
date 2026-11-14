"""Print a one-line summary of a publisher or handler stats file.

    python3 report.py publisher /tmp/publisher-stats.json
    python3 report.py handler /tmp/handler-stats.json sub1
"""
import json
import sys

kind, path = sys.argv[1], sys.argv[2]
d = json.load(open(path))
if kind == "publisher":
    print(f"sent: {d['messages']:,} messages, {d['packets_per_feed']:,} packets per feed")
else:
    name = sys.argv[3] if len(sys.argv) > 3 else "handler"
    m, a, b = d["merged"], d["feeds"]["A"], d["feeds"]["B"]
    print(f"{name}: merged {m['messages']:,} msgs"
          f" | A pkts {a['packets']:,} missing {a['gap_messages']:,} wins {a['wins']:,}"
          f" | B pkts {b['packets']:,} missing {b['gap_messages']:,} wins {b['wins']:,}"
          f" | both-missing gaps {m['gaps']} re-requests {m['retransmit_requests']}"
          f" recovered {m['recovered']:,} lost {m['unrecovered']:,} | kernel buffer drops {d.get('udp_rcvbuf_errors', 0):,}")
    sk = d.get("ab_skew") or {}
    if sk:
        print(f"{name}: A/B skew over {sk['samples']:,} packet pairs: p50 {sk['p50_ms']} ms, p99 {sk['p99_ms']} ms,"
              f" p99.9 {sk['p999_ms']} ms, max {sk['max_ms']} ms")
