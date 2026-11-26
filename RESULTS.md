# TradeHunt — Measured Results

Lab results only. Data is Nasdaq's public TotalView-ITCH 5.0 sample file `itch50_05_15` (opening slice 09:29:30–09:31:00). Lab: GCP `c4a-standard-4` (4 Axion vCPU, 16 GB), 4 × Arista cEOS-lab 4.37.0F (arm64) in Containerlab.

## Before building — feasibility checks (2026-11-13)
- **cEOS forwards PIM-SSM multicast in software:** 2 routers, IGMPv3 (S,G) join, 2,000 of 2,000 packets delivered.
- **Real data source:** IEX historical pcaps are no longer downloadable (IEX: "not currently available for direct download"). Nasdaq's public TotalView-ITCH 5.0 sample files are, and the newest is `itch50_05_15`.
- **Real market-open rate:** 61.6 M messages from 03:01 to 09:31; **peak 698,189 msgs/s at 09:30:00**, a 7× jump in one second. Mostly Add Order (27.2 M) and Delete (25.0 M).
- **Python receive speed:** about 350k packets/s on one core (M4, localhost), enough once symbols are split across 4 channels.
- **GCP:** Terraform plan, ARM `c4a-standard-4` in us-east4, IAP-only SSH, $50 budget alert.

## Day 1 — network and real-data replay (2026-11-13)

### Network
- 7 nodes deployed in **70 s**: a-edge, a-leaf, b-edge, b-leaf, exchange, sub1, sub2. About 6 GB of 15 GB RAM used.
- Each side: PIM neighbour edge↔leaf, an OSPF route to its feed source, PIM-SSM 232/8, IGMPv3, IGMP snooping on the subscriber VLANs (110 / 210).

### What the real data demands
Packets per feed come from batching each channel's messages per millisecond into MoldUDP64 packets of up to 1,400 bytes, across 4 channels.

| Exchange time | Messages/s | Packets/s per feed |
|---|---|---|
| 09:29:58 | 69,698 | 4,640 |
| 09:29:59 | 105,386 | 5,207 |
| **09:30:00** | **698,189** | **20,468** |
| 09:30:01 | 568,871 | 16,105 |
| 09:30:02 | 400,912 | 11,885 |
| 09:30:05 | 207,503 | 6,891 |

### Lab forwarding limit (measured)
cEOS-lab forwards in software (the `Etba` process). Results replaying 09:29:30–09:29:40 (311,129 messages, 32,112 packets per feed):

| Replay speed | Avg / peak packets/s per feed | Received per feed | Loss |
|---|---|---|---|
| 1.0× | ~3,200 / 4,255 | ~21,300 of 32,112 | **~34%**, the same packets on both subscribers, so lost upstream of the leaf |
| 0.5× | ~1,600 / ~2,100 | 32,112 and 32,111 | ≈0 (1 packet) |
| 0.25× | ~800 / ~1,060 | 32,112 | **0** |

At 0.25×, each switch's `Etba` used about 25–33% of a vCPU. **The usable lab rate is about 2,000 packets/s per feed.** Real Arista switches forward in hardware; this limit belongs to the lab.

### The market open, replayed at 1/10 speed ✅
Window 09:29:58–09:30:05; the peak becomes about 2,066 packets/s per feed. The 7× shape of the burst is preserved.

| | Feed A | Feed B |
|---|---|---|
| sub1 | 76,761 / 76,761 packets · 2,440,699 messages · **0 gaps** | 76,761 / 76,761 · 2,440,699 · **0 gaps** |
| sub2 | 76,761 / 76,761 packets · 2,440,699 messages · **0 gaps** | 76,761 / 76,761 · 2,440,699 · **0 gaps** |

- Publisher: 2,440,699 messages in 70.0 s wall time (34,872 msgs/s average). Its worst lag behind schedule was **5.8 ms**, with 0 send errors.
- In plain terms: the open (peak 698K msgs/s in the data) was replayed at 1/10 speed, about 70K msgs/s at peak, with zero gaps on both feeds. The lab did **not** replay 698K msgs/s.

### Note on one discarded run
One 0.1× run was invalid: a run interrupted by a dropped SSH session kept running in the containers and overlapped the next one. The lab scripts now stop stale feed processes and delete old results before each run. (Day 1 was measured with an early counting script; the feed handler now does that job.)

## Day 2 — feed handler, retransmission, monitoring (2026-11-13)

Built `feed/handler.py` (A/B arbitration, per-feed and merged gap tracking, MoldUDP64 re-requests, Prometheus metrics), `feed/retransmitter.py` (simple MoldUDP64 resend server), Prometheus with 4 alert rules, and a provisioned Grafana dashboard.

### Arbitration and retransmission on real data
Window 09:29:58–09:30:01 at 0.1×: 873,273 messages, 30,525 packets per feed. Loss is injected with `tc netem` on the exchange's feed interfaces. Results are the same on sub1 and sub2.

| Case | What was lost | Merged output | Re-requests | Recovered | Lost |
|---|---|---|---|---|---|
| Clean | nothing | **873,273 / 873,273** | 0 | 0 | 0 |
| 2% loss, feed A only | 565 A packets (16,040 msgs) | **873,273 / 873,273**, all covered by feed B | **0** | 0 | 0 |
| 2% loss, both feeds | ~600 packets per feed | **873,273 / 873,273** | 10 | 268 msgs | **0** |

- The both-feed case had **10 holes missing on both feeds**, close to the ~12 expected from independent 2% loss (0.02 × 0.02 × 30,525).
- Arbitration share in clean runs: **feed B delivered first about 64% of the time**, even though the publisher sends A first. The B path is slightly faster in this lab.

### A/B skew (how much later the second copy arrives)
| Condition | p50 | p99 | p99.9 | max |
|---|---|---|---|---|
| Clean replay of the open, 0.1× | 4.4–4.7 ms | 43–73 ms | 52–97 ms | 54–101 ms |
| Steady pre-open stream, 0.1× | 1.9 ms | 10.7 ms | 14 ms | 15 ms |
| With `tc netem` active | 5–5.5 ms | 112–187 ms | 123–191 ms | 126–193 ms |

**Arbitration window set to 150 ms** (`--gap-timeout`). The first version waited 50 ms and requested 294 messages that feed B was still about to deliver. Raising the window to the measured skew removed those false requests (case 2: 294 → 0).

### Bugs found and fixed
1. **Double counting at a new replay pass** (found in code review before the first run): feed B's copy of sequence 1 could reset a channel twice. Now a channel resets only after an end-of-session marker.
2. **Tail loss not detected** (found by the dual-loss test): packets lost on both feeds at the end of a channel had nothing after them to reveal the hole, so the merged output came up 35 messages short while reporting "lost 0". Heartbeats and end-of-session markers, which carry the next sequence number, now expose the hole, and it is recovered (873,238 → 873,273).

### Monitoring
- Prometheus scrapes both handlers every 1 s. Alert rules: `FeedSideSilent`, `MergedGap`, `UnrecoveredMessages`, `UdpReceiveBufferOverflow`.
- Grafana 11.4 dashboard "TradeHunt — A/B market-data feeds" is provisioned automatically (8 panels).

### Silent feed detection (preview of incident 8)
Feed A cut by shutting `a-edge Ethernet2` during a continuous replay:
- `FeedSideSilent` **pending after 4.5 s, firing on both servers after 6.1 s**
- Merged output during the outage: **0 gaps, 0 lost**, because arbitration hid the loss from consumers
- After `no shutdown`, the alert **cleared in 7.0 s**

## Day 3 — diagnostic tool and incidents 1–4 (2026-11-13)

Built `tools/tradehunt.py` (checks feed delivery from the consumer side, then walks the path back: host → leaf → edge → exchange), `lab/incident.sh` (inject and fix incidents 1–4), `lab/blind-run.sh` (random incident, automatic scoring), and KB articles `kb/01`–`kb/04`. Scope is now 6 incidents.

### Each incident, injected and diagnosed during a continuous replay
| # | Incident | What `tradehunt` found | Diagnosis time |
|---|---|---|---|
| 1 | Publisher TTL = 1 | All 4 feeds silent; packets leave the exchange with `ttl 1` (need ≥ 3) | 10.8 s |
| 2 | `rp_filter` on sub1 | Only sub1 feed B silent; route to 10.2.0.10 via eth0, `rp_filter` 1/1, **packets on the wire: yes** | 2.6 s |
| 3 | No IGMP querier/router on Vlan110 | Feed A silent on both servers; PIM/IGMP on Vlan110: NO, groups learned: 0 | 5.7 s |
| 4 | Leftover blackhole route on a-leaf | Feed A silent on both servers; RPF interface for 10.1.0.10 is **Null0** (expected Ethernet1) | 5.7 s |

All feeds were delivering again after each fix.

### Blind runs: **6 of 6 correct**
| Run | Injected | Diagnosed | Correct | Seconds |
|---|---|---|---|---|
| 1 | rp_filter | rp_filter | yes | 1.1 |
| 2 | rpf | rpf | yes | 5.1 |
| 3 | ttl | ttl | yes | 10.7 |
| 4 | rpf | rpf | yes | 5.2 |
| 5 | igmp_querier | igmp_querier | yes | 5.3 |
| 6 | rp_filter | rp_filter | yes | 2.6 |

The time is the tool's own run time, after a 20 s wait that lets the symptom show up in the 10 s rate window. TTL takes longest because it captures packets at the exchange.

### Found while building
- **A static route whose next hop doesn't resolve is not installed by EOS.** The first version of incident 4 (`ip route 10.1.0.0/24 10.1.10.99`) sat in the config and broke nothing. It now uses a `Null0` blackhole route, which always installs.
- EOS has no `show ip rpf`. The tool reads the RPF interface from `show ip route <source>`, which is the route PIM uses for its RPF check.

## Day 4 — incidents 5 and 6, blind runs across all 6 (2026-11-14)

Added incident 5 (sub1's feed handler restarted with a 2 KB `SO_RCVBUF`, which the kernel rounds to 4 KB) and incident 6 (a-edge Ethernet2, the A-side uplink, shut down). `tradehunt` gained an interface check along the feed path, and a socket-buffer check (kernel drops over 30 s from Prometheus, plus the feed sockets' buffer size from `ss`).

### Incident 5 — socket buffer too small, at the real open
Window 09:29:58–09:30:01, 0.1×, 873,273 messages, tiny buffer on both servers:

| | sub1 | sub2 |
|---|---|---|
| Kernel drops (full receive buffer) | 7,950 packets | 8,476 packets |
| Messages missing on feed A / feed B alone | 148,014 / 135,235 | 155,167 / 146,225 |
| Holes missing on **both** feeds | 286 | 247 |
| Recovered by retransmission during the run | 10,049 msgs | 8,673 msgs |
| Delivered in order when the replay ended | 365,228 of 873,273 | 353,383 of 873,273 |

With the default 4 MB buffer, the same replay has **0 drops and 0 gaps** (Day 2). Both feeds overflow during the same bursts, so arbitration can't help, and retransmission (one packet per reply) falls behind. The remaining messages were still waiting in the retransmission backlog when the replay ended, so they were neither delivered nor counted as lost.

**Live diagnosis:** the first version sampled the kernel counter for 3 s and saw nothing, because drops come in bursts at pre-open rates. It now uses a 30 s count and also flags any feed socket buffer under 256 KB: `kernel dropped 41 packets on full buffers in the last 30 s; smallest feed socket buffer 4 KB` → **rcvbuf**.

### Incident 6 — feed A silently dead
| Measurement | Result |
|---|---|
| `FeedSideSilent` firing on both servers | **7.0 s** after the shutdown |
| Merged gaps + lost messages during the outage | **0** before, **0** after |
| `tradehunt` | `a-edge Et2 disabled, a-leaf Et1 notconnect` → **link_down**, ranked above the RPF failure it causes, in 5.9 s |
| Alert cleared after `no shutdown` | 11.7 s |

### Blind runs across incidents 1–6: **12 of 12 correct**
| Run | Injected | Diagnosed | Seconds |
|---|---|---|---|
| 1 | 5 rcvbuf | rcvbuf | 0.1 |
| 2 | 4 rpf | rpf | 5.9 |
| 3 | 3 igmp_querier | igmp_querier | 5.9 |
| 4 | 4 rpf | rpf | 5.9 |
| 5 | 4 rpf | rpf | 5.2 |
| 6 | 6 link_down | link_down | 5.3 |
| 7 | 5 rcvbuf | rcvbuf | 0.1 |
| 8 | 5 rcvbuf | rcvbuf | 0.1 |
| 9 | 3 igmp_querier | igmp_querier | 5.2 |
| 10 | 2 rp_filter | rp_filter | 2.7 |
| 11 | 4 rpf | rpf | 5.7 |
| 12 | 4 rpf | rpf | 5.9 |

Incident 1 (TTL) didn't come up in this random set; it was covered in the Day 3 blind runs. **Combined: 18 of 18 correct, and every incident type appeared at least once.**

## Day 5 — feedscope: feed quality and microbursts from any capture (2026-11-25)

Built `tools/feedscope.py`, which reads a pcap or pcapng of MoldUDP64 feeds and reports gaps, messages missing on both sides, silent sides, A/B skew, microbursts and per-socket receive-buffer needs. Built `tools/mkcapture.py`, which writes captures from the real Nasdaq data through the publisher's own packet plan. It stamps each packet with the exchange time of its last message and injects seeded faults, so every expected number is known.

### The real open at full speed (no lab limit)
Window 09:29:58–09:30:05 at 1.0×: 2,440,699 messages, 76,761 packets per feed. The lab could only replay this at 0.1×; a capture has no forwarding limit.

| Window | Peak packets/s per feed |
|---|---|
| average over the window | 10,965 |
| 1 s | 20,657 |
| 100 ms | 28,650 |
| 10 ms | 38,400 |
| **1 ms** | **47,000** (2.3× the busiest second, 4.3× the average) |
| 100 µs | 110,000 |

**Receive buffer for the busiest socket** (232.1.1.1:30001, busiest second 5,273 packets/s), for a consumer that reads at a steady rate:

| Consumer reads | Peak backlog | Kernel memory needed (~2.3 KB per packet) |
|---|---|---|
| 5,273 pkts/s (keeps up with the busiest second) | 509 packets | ~1 MB |
| 10,546 pkts/s (2×) | 11 packets | ~25 KB |
| 26,365 pkts/s (5×) | 4 packets | ~9 KB |

Sizing a consumer or its buffer from per-second rates fails at the open: keeping up with the busiest second still leaves a 509-packet backlog. These are minimums for a steady reader; one that stalls (a busy CPU, a GC pause) needs more. That fits incident 5, where a 4 KB buffer, room for about one packet, dropped ~8,000 packets per server even at 0.1×.

Caveat: below 1 ms, the shape depends on packetization. The publisher batches each channel's messages per millisecond, and Nasdaq's own packetizer will differ.

### Validation: feedscope finds exactly what was injected
14 tests (`python3 -m unittest discover -s tests`). The synthetic tests use hand-built captures; the real-data tests use the Nasdaq slice 09:29:58–09:30:01 at 1.0×.

| Injected | feedscope reported |
|---|---|
| 2% packet loss on each side, plus 0–0.3 ms jitter on B | Gap messages per side **equal to the injected loss**; messages and holes missing on both sides **equal to the packets dropped on both** |
| 0.4 ms delay + 0–0.3 ms jitter on B | A/B skew between 0.4 and 0.7 ms; A first 100%; reordered B packets counted as late, **not as loss** |
| Side A killed at 1.5 s | A silent from 1.5 s, flagged as hidden by arbitration; 0 messages missing on both sides |
| Tail loss on both sides | Found through the end-of-session marker |
| Same capture as pcap and pcapng | Identical results |

Full-open fault run (2% loss on both, B delayed 0.4 + 0–0.3 ms, A killed at 4 s):
- **B:** injected 49,063 lost messages and found **49,063**, even though jitter reordered 7,293 B packets.
- **A:** injected 28,470 and found 28,451. The other 19 were dropped right before A died, so no later A packet reveals them; no tool can see that loss from the capture alone.
- **Silent feed:** A was reported silent from **4.0 s**, with B still sending.

Analysis speed: 153,530 packets in 0.8 s on an M4 Mac.

### Bugs found while building
1. **Reordering counted as loss** (found by the full-open fault run): jitter made later packets arrive first, and the first version counted every jump in sequence numbers as a gap: 318,507 "lost" B messages against 49,063 injected. Gaps are now counted at the end from what actually arrived, and reordered packets are reported separately as `late`.
2. **pcapng timestamps 1 ns early** (found by the pcap-vs-pcapng test): floating-point conversion put some packets in the wrong time window. It now uses exact integer arithmetic.
3. **Captures that start mid-stream** (found by the lab capture below): the first version counted from sequence 1, so the 51,182 messages sent before the capture started showed up as "missing on both sides". Counting now starts at the first sequence number in the capture.

### On a real capture from the lab network
`tcpdump -i any` on sub1 for 45 s during the continuous 0.1× replay. Feed A's uplink (a-edge Ethernet2) was shut at 12 s and brought back at 30 s. The capture is 19,066 packets in Linux cooked v2 format, which the generated captures never used.

| sub1 during the same outage | Lab feed handler (Prometheus) | feedscope (capture) |
|---|---|---|
| Feed A messages missed | 62,737 | 62,704 |
| Feed B messages missed | 0 | 0 |
| Messages missing on both feeds | 0 | 0 |

The 0.05% difference is most likely Prometheus `increase()`, which estimates over a 4-minute window instead of counting exactly.

- Feed A **silent from t = 12.07 s**, when the link was shut, and flagged as hidden by arbitration.
- Feed A stayed **silent for 23.4 s**, though the link was down for about 18 s: feed A took about 5 s to flow again after `no shutdown`, while OSPF and PIM re-converged. This fits the 7.0–11.7 s it took the alert to clear on Days 2 and 4.
- A/B skew p50 3.7–5.3 ms per channel, p99 10.9–12.2 ms; first arrivals split 45–68% A by channel.

### Not yet done
- Captures from other exchanges' MoldUDP64 feeds: only TradeHunt's session naming and groups have been tested.

### Lab fix
`lab/deploy.sh` waited forever when stopped switch containers from an earlier VM shutdown blocked the deploy, and it hid containerlab's errors. It now starts from a clean destroy, shows deploy errors, and fails with a message if a switch does not boot within 5 minutes or routing does not come up within 200 s.

## Summary (measured, lab only)
- Replayed the Nasdaq open (peak 698K msgs/s in the data) at 1/10 speed: **2,440,699 messages, 0 gaps** on both feeds.
- 2% loss on both feeds: **873,273 of 873,273 messages delivered**, with retransmission recovering what both feeds lost.
- Silent feed detected in **6.1–7.0 s** while clients lost **0** messages.
- Socket buffer sizing at the open: **~8,000 kernel drops per server with 4 KB → 0 with 4 MB**.
- Diagnostic tool: **correct root cause in 18 of 18 blind runs** across 6 incident types.
- feedscope on the real open at full speed: busiest 1 ms at **47,000 packets/s** per feed, 2.3× the busiest second; a consumer that keeps up with the busiest second still needs **~1 MB** of buffer per socket. Injected loss found **exactly**, including under reordering; on a real lab capture, feed A loss within 0.05% of the feed handler's own count.
