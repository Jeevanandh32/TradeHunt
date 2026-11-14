# KB-03: Feed A stops for all servers on a VLAN — no IGMP querier/router

**Symptom (client ticket):** "Feed A stopped on all our servers after the network change."
**Impact:** Every server on VLAN 110 lost feed A. Feed B still delivers, so there are no gaps yet but no redundancy either.

## Diagnosis
1. Scope: **feed A is silent on every server** on the same VLAN, while feed B is fine. Look at the shared A-side switch for that VLAN.
2. `show ip igmp groups` on a-leaf: **no 232.x groups learned on Vlan110**. The switch has stopped hearing the servers' joins.
3. `show ip pim interface`: **Vlan110 is missing**. PIM and IGMP are off on the SVI, so there is no IGMP querier and no multicast router for the VLAN.
4. `tradehunt diagnose` reports `a-leaf IGMP querier/router on Vlan110: PIM/IGMP on Vlan110: NO; groups learned: 0`.

## Root cause
`pim ipv4 sparse-mode` was removed from `interface Vlan110`. That disabled IGMP on the VLAN (no querier) and removed the VLAN from the multicast outgoing list.

## Fix
`interface Vlan110` → `pim ipv4 sparse-mode`.

## Prevention
Include `show ip pim interface` and `show ip igmp groups` in every change's pre- and post-checks.

**Lab note:** in this lab the leaf is both the router and the snooping switch, so the feed stops immediately. On a network with a separate router and a snooping switch with no querier, it typically stops a few minutes later, when the memberships time out.

## Lab evidence
Feed A went silent on both servers. `tradehunt` reported `PIM/IGMP on Vlan110: NO; groups learned: 0` and named the root cause in **5.3–5.7 s**.
