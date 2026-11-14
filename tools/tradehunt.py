#!/usr/bin/env python3
"""tradehunt: diagnose market-data feed problems in the TradeHunt lab, the way a support engineer would.

Runs on the lab VM. It looks at the feeds from the consumer side first: are messages arriving on
A and B, and are any being lost? For a silent feed it walks the path back to the source (server,
leaf, edge, exchange); for a feed that delivers but loses messages it checks the receiving server.
Each check reports what it saw; the failing check nearest the source is the likely root cause.

    sudo python3 tradehunt.py diagnose            # human-readable report
    sudo python3 tradehunt.py diagnose --json     # for scripts (blind runs)
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROM = "http://localhost:9090/api/v1/query"
SIDES = {"A": {"n": 1, "leaf": "a-leaf", "edge": "a-edge", "vlan": "Vlan110", "dev": "eth1", "source": "10.1.0.10"},
         "B": {"n": 2, "leaf": "b-leaf", "edge": "b-edge", "vlan": "Vlan210", "dev": "eth2", "source": "10.2.0.10"}}
SUBS = ["sub1", "sub2"]
MIN_TTL = 3  # exchange -> edge -> leaf -> host: two routed hops, so TTL must start at 3 or more


def sh(container, cmd, timeout=15):
    r = subprocess.run(["docker", "exec", f"clab-tradehunt-{container}", "sh", "-c", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout


def eos(switch, cmd):
    r = subprocess.run(["docker", "exec", f"clab-tradehunt-{switch}", "Cli", "-p", "15", "-c", cmd],
                       capture_output=True, text=True, timeout=20)
    return r.stdout


def prom(query):
    url = PROM + "?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)["data"]["result"]


findings = []


def finding(check, ok, detail, cause=None, fix=None):
    findings.append({"check": check, "ok": ok, "detail": detail, "cause": cause, "fix": fix})


def feed_rates():
    """messages/s per (server, feed) over the last 10 s, from the handlers via Prometheus."""
    rates = {(s, f): 0.0 for s in SUBS for f in SIDES}
    for r in prom("sum by(host, feed) (rate(tradehunt_feed_messages_total[10s]))"):
        rates[(r["metric"]["host"], r["metric"]["feed"])] = float(r["value"][1])
    return rates


def check_exchange_ttl(side):
    out = sh("exchange", f"timeout 3 tcpdump -c 3 -nv -i {SIDES[side]['dev']} udp and dst net 232.0.0.0/8 2>/dev/null")
    ttls = [int(t) for t in re.findall(r"ttl (\d+)", out)]
    if not ttls:
        finding(f"exchange egress TTL (feed {side})", None, "no feed packets seen leaving the exchange")
        return
    ok = min(ttls) >= MIN_TTL
    finding(f"exchange egress TTL (feed {side})", ok, f"packets leave with TTL {min(ttls)} (need >= {MIN_TTL})",
            None if ok else "ttl", None if ok else "raise the publisher's IP_MULTICAST_TTL (e.g. 16)")


def check_rpf(side):
    s = SIDES[side]
    # PIM's RPF check uses the unicast route back to the source
    out = eos(s["leaf"], f"show ip route {s['source']}")
    m = re.search(r"via \S+, (\S+)", out) or re.search(r"directly connected, (\S+)", out)
    iface = m.group(1) if m else "no route"
    ok = iface.startswith("Ethernet1")
    finding(f"{s['leaf']} RPF to feed {side} source", ok, f"RPF interface for {s['source']} is {iface} (expected Ethernet1)",
            None if ok else "rpf", None if ok else f"fix the route to {s['source']} on {s['leaf']} (look for a stray static route)")


def check_igmp_router(side):
    s = SIDES[side]
    pim = eos(s["leaf"], "show ip pim interface")
    groups = eos(s["leaf"], "show ip igmp groups")
    has_pim = s["vlan"] in pim
    joined = len(re.findall(r"^232\.\S+\s+" + s["vlan"], groups, re.M))
    ok = has_pim and joined > 0
    finding(f"{s['leaf']} IGMP querier/router on {s['vlan']}", ok,
            f"PIM/IGMP on {s['vlan']}: {'yes' if has_pim else 'NO'}; groups learned: {joined}",
            None if ok else "igmp_querier", None if ok else f"enable PIM/IGMP (querier) on {s['vlan']}")


def check_host_rpfilter(sub, side):
    s = SIDES[side]
    route_dev = re.search(r"dev (\S+)", sh(sub, f"ip route get {s['source']}") or "")
    route_dev = route_dev.group(1) if route_dev else "?"
    rpf = sh(sub, f"sysctl -n net.ipv4.conf.all.rp_filter net.ipv4.conf.{s['dev']}.rp_filter").split()
    strict = "1" in rpf
    arriving = "length" in sh(sub, f"timeout 2 tcpdump -c 1 -n -i {s['dev']} udp and dst net 232.0.0.0/8 2>/dev/null")
    bad = strict and route_dev != s["dev"]
    finding(f"{sub} reverse-path filter (feed {side})", not bad,
            f"route to {s['source']} via {route_dev} (feed arrives on {s['dev']}), rp_filter all/{s['dev']}={'/'.join(rpf)},"
            f" packets on the wire: {'yes' if arriving else 'no'}",
            "rp_filter" if bad else None,
            f"add a route to {s['source']} via {s['dev']} or set rp_filter=2 (loose)" if bad else None)


def check_links(side):
    """Every interface on the feed's path should be up: exchange-facing, transit and server-facing ports."""
    s = SIDES[side]
    down = []
    for switch, ports in ((s["edge"], ("Et1", "Et2")), (s["leaf"], ("Et1", "Et2", "Et3"))):
        for line in eos(switch, "show interfaces status").splitlines():
            parts = line.split()
            if parts and parts[0] in ports and "connected" not in parts:
                state = next((p for p in parts if p in ("disabled", "notconnect", "errdisabled")), "down")
                down.append(f"{switch} {parts[0]} {state}")
    ok = not down
    finding(f"feed {side} path interfaces", ok, "all up" if ok else "DOWN: " + ", ".join(down),
            None if ok else "link_down", None if ok else f"bring the interface back up ({down[0]}) and find out why it went down")


def gap_rates():
    """messages each feed alone was missing over the last 20 s, per (server, feed)."""
    gaps = {(s, f): 0.0 for s in SUBS for f in SIDES}
    for r in prom("sum by(host, feed) (increase(tradehunt_feed_gap_messages_total[20s]))"):
        gaps[(r["metric"]["host"], r["metric"]["feed"])] = float(r["value"][1])
    return gaps


MIN_RCVBUF = 256 * 1024  # below this a feed socket can't absorb even a short burst


def check_host_buffers(sub):
    """Kernel UDP drops on a full socket receive buffer, and how big the feed sockets' buffers are."""
    # drops come in bursts, so count over 30 s rather than sampling a few seconds
    r = prom(f'increase(tradehunt_udp_rcvbuf_errors_total{{host="{sub}"}}[30s])')
    drops = round(float(r[0]["value"][1])) if r else 0
    rb = [int(x) for x in re.findall(r"rb(\d+)", sh(sub, "ss -uamn 'src 232.0.0.0/8'"))]
    smallest = min(rb) if rb else None
    small = smallest is not None and smallest < MIN_RCVBUF
    ok = drops == 0 and not small
    size = f"{smallest // 1024} KB" if smallest is not None else "?"
    finding(f"{sub} socket receive buffers", ok, f"kernel dropped {drops} packets on full buffers in the last 30 s;"
            f" smallest feed socket buffer {size}",
            None if ok else "rcvbuf", None if ok else "raise the handler's SO_RCVBUF (and net.core.rmem_max) to absorb bursts")


def firing_alerts():
    return [f"{r['metric']['alertname']}({r['metric'].get('host', '')}{':' + r['metric']['feed'] if 'feed' in r['metric'] else ''})"
            for r in prom('ALERTS{alertstate="firing"}')]


def diagnose():
    t0 = time.time()
    rates, gaps = feed_rates(), gap_rates()
    for (srv, f), r in sorted(rates.items()):
        finding(f"{srv} feed {f} delivery", r >= 1 and gaps[(srv, f)] == 0,
                f"{r:,.0f} msgs/s, {gaps[(srv, f)]:,.0f} messages missing in the last 20 s")
    silent = [k for k, r in rates.items() if r < 1]
    gappy = [k for k, g in gaps.items() if g > 0 and rates[k] >= 1]

    # silent feeds: walk the path back from consumer to source
    for side in SIDES:
        silent_on = [srv for srv in SUBS if (srv, side) in silent]
        if not silent_on:
            continue
        for srv in silent_on:
            check_host_rpfilter(srv, side)
        check_igmp_router(side)
        check_rpf(side)
        check_links(side)
        check_exchange_ttl(side)

    # feeds that deliver but keep losing messages: look at the receiving host
    for srv in sorted({srv for srv, _ in gappy}):
        check_host_buffers(srv)

    causes = [f["cause"] for f in findings if f["cause"]]
    # the failure nearest the source explains everything downstream of it
    priority = ["ttl", "link_down", "rpf", "igmp_querier", "rp_filter", "rcvbuf"]
    root = next((c for c in priority if c in causes), None)
    return {"silent_feeds": [f"{srv}:{f}" for srv, f in sorted(silent)],
            "lossy_feeds": [f"{srv}:{f}" for srv, f in sorted(gappy)],
            "alerts": firing_alerts(), "root_cause": root, "findings": findings,
            "seconds": round(time.time() - t0, 1)}


def report(result):
    print("TradeHunt diagnosis")
    print("=" * 60)
    for f in result["findings"]:
        mark = {True: "OK  ", False: "FAIL", None: "??  "}[f["ok"]]
        print(f"[{mark}] {f['check']}: {f['detail']}")
    print("-" * 60)
    if result["alerts"]:
        print(f"Firing alerts: {', '.join(result['alerts'])}")
    if not result["silent_feeds"] and not result["lossy_feeds"]:
        print("All feeds delivering without loss. No fault found.")
    else:
        if result["silent_feeds"]:
            print(f"Silent feeds: {', '.join(result['silent_feeds'])}")
        if result["lossy_feeds"]:
            print(f"Feeds losing messages: {', '.join(result['lossy_feeds'])}")
        fix = next((f["fix"] for f in result["findings"] if f["cause"] == result["root_cause"]), None)
        print(f"Likely root cause: {result['root_cause'] or 'not identified'}")
        if fix:
            print(f"Suggested fix: {fix}")
    print(f"(diagnosis took {result['seconds']} s)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["diagnose"])
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    res = diagnose()
    print(json.dumps(res, indent=2)) if a.json else report(res)
    sys.exit(0)
