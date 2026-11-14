# KB-05: Gaps at the market open on one server — socket receive buffer too small

**Symptom (client ticket):** "We see gaps every morning at the open on sub1. The rest of the day is fine."
**Impact:** Messages are lost on **both** feeds at the same moments, so A/B arbitration can't fill them. Retransmission has to recover them, and it can't keep up with loss on this scale.

## Diagnosis
1. Scope: **both feeds on the same server** lose messages, and **other servers are fine**. The network delivers, so look at the receiving host.
2. Kernel counters: `netstat -su` / `/proc/net/snmp` → **`RcvbufErrors` keeps climbing**. The kernel is discarding packets because a socket's receive buffer is full.
3. Buffer size: `ss -uam` on the feed sockets shows **`rb4096`**, a 4 KB buffer, room for only a couple of packets.
4. Timing: the drops line up with bursts. At the open, traffic jumps 7× in one second (105K → 698K msgs/s in the real data).
5. `tradehunt diagnose` reports `sub1 socket receive buffers: kernel dropped N packets on full buffers in the last 30 s; smallest feed socket buffer 4 KB`, and the `UdpReceiveBufferOverflow` alert fires.

## Root cause
The feed handler's `SO_RCVBUF` is far too small to absorb bursts while the application is busy for a few milliseconds.

## Fix
Raise `SO_RCVBUF` (TradeHunt's default is 4 MB) and make sure `net.core.rmem_max` allows it. Then restart the handler.

## Prevention
Size buffers for the worst burst, not the average rate. Alert on `RcvbufErrors` growth before clients notice gaps.

## Lab evidence
Replay of the real open (09:29:58–09:30:01, 0.1×, 873,273 messages):

| Buffer | Kernel drops per server | Messages missing per feed | Both-feed gaps | In-order delivery at end of replay |
|---|---|---|---|---|
| 4 KB | 7,950 / 8,476 | 135,235–155,167 | 247–286 (10,049 / 8,673 msgs recovered) | 365,228 / 353,383 of 873,273, with a retransmission backlog still open |
| 4 MB (default) | **0** | **0** | **0** | **873,273 of 873,273** |

Lesson: retransmission is for occasional loss. One resend reply carries at most one packet, so it can't repair a buffer that overflows on every burst.
