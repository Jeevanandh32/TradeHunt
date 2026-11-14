# TradeHunt — 3-minute demo script

For recording the demo video. Run everything on the VM, with Grafana open in a browser through the tunnel (see README).

| Time | Show | Say |
|---|---|---|
| 0:00 | README diagram | "Exchanges send market data twice, on feeds A and B. Firms keep whichever copy arrives first. TradeHunt rebuilds that on Arista switches and replays real Nasdaq data through it." |
| 0:20 | `lab/feeds.sh start`, then Grafana | "Real Nasdaq ITCH data, MoldUDP64 packets, both feeds flowing to both trading servers." Point at the matching A and B rates. |
| 0:45 | `lab/incident.sh inject 6`, then Grafana | "I cut the A-side uplink. Clients lose nothing, because feed B covers it. That's exactly why it's dangerous." Wait for **FeedSideSilent** to fire (~7 s). |
| 1:10 | `tradehunt.py diagnose` | "The tool walks the path back and names the cause: a-edge Ethernet2 is down. The missing route is a symptom, not the cause." |
| 1:30 | `lab/incident.sh fix 6` | The feed comes back and the alert clears. |
| 1:40 | `lab/incident.sh inject 2`, then `diagnose` | "A host problem: packets reach the NIC, but Linux reverse-path filtering drops them. The tool shows packets on the wire, a strict rp_filter and the wrong route." |
| 2:05 | `lab/incident.sh fix 2` | |
| 2:10 | `kb/05-socket-buffer-overflow.md` lab-evidence table | "At the open, a 4 KB buffer meant ~8,000 kernel drops per server, and retransmission couldn't keep up. A 4 MB buffer: zero." |
| 2:35 | RESULTS.md blind runs | "The tool found the correct root cause in 18 of 18 blind runs across all six incidents." |
| 2:50 | README results table | "All measured in a lab on GCP built with Terraform. Numbers, scripts and KB articles are in the repo." |
