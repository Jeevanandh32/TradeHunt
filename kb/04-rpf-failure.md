# KB-04: Feed A stops after a routing change — RPF failure

**Symptom (client ticket):** "Feed A died right after last night's routing change."
**Impact:** Feed A is lost on every server behind a-leaf. Feed B is unaffected.

## Diagnosis
1. Scope: feed A is silent on all servers. Feed B is fine. The joins are still there (`show ip igmp groups` lists the groups), so the switch wants the traffic.
2. `show ip route 10.1.0.10` on a-leaf: the route PIM uses for its RPF check now points to **Null0**, but the feed arrives on **Ethernet1**. Multicast that fails the RPF check is dropped, and no PIM join goes upstream.
3. `show running-config | include ip route`: a leftover **static blackhole route** `ip route 10.1.0.0/24 Null0` (AD 1) overrides the OSPF route via a-edge.
4. `tradehunt diagnose` reports `a-leaf RPF to feed A source: RPF interface for 10.1.0.10 is Null0 (expected Ethernet1)`.

## Root cause
A static blackhole route to the feed source subnet, left behind after maintenance, changed the RPF interface.

## Fix
`no ip route 10.1.0.0/24 Null0`. RPF returns to Ethernet1 via OSPF and the (S,G) state rebuilds.

## Prevention
Check the route to every feed source (`show ip route <source>`) before and after routing changes.

**Lab note:** a static route to a next hop that doesn't answer ARP (for example `10.1.10.99`) stays in the config but is **not installed**, so it doesn't break RPF. The first version of this incident did nothing for exactly that reason. A blackhole (`Null0`) route always installs.

## Lab evidence
Feed A went silent on both servers. `tradehunt` reported `RPF interface for 10.1.0.10 is Null0 (expected Ethernet1)` and named the root cause in **5.1–5.7 s**.
