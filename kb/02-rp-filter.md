# KB-02: No data on feed B on one server — Linux reverse-path filter

**Symptom (client ticket):** "Feed B is dead on sub1. Feed A is fine, and other servers get feed B."
**Impact:** sub1 has lost its redundancy. A/B arbitration hides it, so the client sees no gaps yet, but a feed A failure would now be a full outage.

## Diagnosis
1. Scope: only **sub1 / feed B** is silent. The network delivers feed B to other servers, so look at the host.
2. Is the traffic reaching the NIC? `tcpdump -n -i eth2 udp and dst net 232.0.0.0/8`. **Yes, packets arrive**, but the application gets none. That points to the kernel dropping them.
3. `ip route get 10.2.0.10`: the route back to the feed B source points out **eth0 (management)**, not eth2, where the feed arrives.
4. `sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.eth2.rp_filter` is **1 (strict)**. The kernel drops packets whose source isn't reachable via the arriving interface.
5. `tradehunt diagnose` flags `sub1 reverse-path filter (feed B)`.

## Root cause
Strict reverse-path filtering, plus a missing route back to the feed B source network via eth2.

## Fix
Add `ip route 10.2.0.0/16 via 10.2.10.1 dev eth2`, or set `rp_filter=2` (loose) on the feed interfaces.

## Prevention
Standard host build for feed servers: routes to each feed's source network via that feed's interface, with loose `rp_filter`.

## Lab evidence
Only sub1's feed B went silent. The other server and feed A were unaffected, and the merged output on sub1 was still complete thanks to feed A. `tradehunt` showed the route via eth0, `rp_filter` 1/1, and **packets on the wire: yes**. Root cause named in **1.1–2.6 s**.
