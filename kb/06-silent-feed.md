# KB-06: Feed A silently dead — arbitration hides the outage

**Symptom:** none from the client. Every message still arrives, via feed B. Monitoring raises `FeedSideSilent`.
**Impact:** No data loss **yet**, but the firm is running with **no redundancy**. If feed B has a problem now, it's a full outage. This is the incident that turns into a major outage when it goes unnoticed.

## Diagnosis
1. Alert: `FeedSideSilent` fires for feed A on **every server**, while feed B is healthy.
2. Scope: all servers lost the same feed, so the fault is on the shared A-side path, not on a host.
3. `show interfaces status` on the A-side switches: **a-edge Ethernet2 disabled** (administratively down), so a-leaf Ethernet1 is **notconnect**.
4. Side effects that look like other faults: a-leaf loses its OSPF route to the source, so the RPF check fails. The link is the cause; RPF is a symptom.
5. `tradehunt diagnose` reports `feed A path interfaces: DOWN: a-edge Et2 disabled, a-leaf Et1 notconnect` → root cause **link_down**, ranked above the RPF symptom.

## Root cause
The A-side uplink between a-edge and a-leaf was shut down.

## Fix
`interface Ethernet2` → `no shutdown` on a-edge. OSPF and PIM rebuild the path and feed A resumes.

## Prevention
Alert on a silent feed side, not just on gaps after the merge. A merged-output monitor alone would never see this.

## Lab evidence
| Measurement | Result |
|---|---|
| `FeedSideSilent` firing on both servers after the shutdown | **6.1 s** (Day 2), **7.0 s** (Day 4) |
| Messages lost by clients during the outage | **0**, because feed B covered everything |
| Alert cleared after `no shutdown` | 7.0 s (Day 2), 11.7 s (Day 4) |
| `tradehunt` diagnosis | link_down, in 5.9 s |
