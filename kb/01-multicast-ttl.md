# KB-01: No market data on any feed — multicast TTL too low

**Symptom (client ticket):** "We're getting no data at all, on either feed."
**Impact:** Total outage for every subscriber. A/B redundancy doesn't help, because both feeds have the same fault.

## Diagnosis
1. Confirm the scope: the handler shows **both feeds at 0 msgs/s on every server**, so the problem is common to both paths. Suspect the source.
2. Capture at the source: `tcpdump -nv -i eth1 udp and dst net 232.0.0.0/8`. The packets **are** leaving, but with **`ttl 1`**.
3. Count the routed hops: exchange → edge (routed) → leaf (routed) → host. Each router decrements TTL, so **TTL 1 dies at the first router**.
4. `tradehunt diagnose` reports `exchange egress TTL (feed A/B): packets leave with TTL 1 (need >= 3)`.

## Root cause
The publisher sets `IP_MULTICAST_TTL = 1`, the Linux default for multicast sockets.

## Fix
Set the multicast TTL above the number of routed hops (TradeHunt uses 16) and restart the publisher.

## Prevention
Treat the publisher's TTL as a reviewed config value. Check egress TTL in a pre-production capture.

## Lab evidence
Injected during a continuous replay: all 4 feeds went silent. `tradehunt` saw `ttl 1` on packets leaving the exchange and named the root cause in **10.7–10.8 s** (it captures at the source, so it's the slowest check). After the fix, all feeds were delivering again.
