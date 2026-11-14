"""TradeHunt feed handler: what a trading server runs to consume the A/B market-data feeds.

What it does, per channel:
  1. Joins the channel on BOTH feeds (IGMPv3 source-specific join: A on eth1, B on eth2).
  2. A/B arbitration: keeps one "next expected sequence number". Whichever feed delivers a range
     first wins; the other feed's copy is a duplicate and is dropped.
  3. Gap recovery: if a range is missing from both feeds for --gap-timeout, it asks the exchange's
     MoldUDP64 retransmission server for it, and gives up after --max-retries.
  4. Serves Prometheus metrics on --metrics-port and writes a JSON summary to --stats.

    python3 handler.py --name sub1 --local-a 10.1.10.11 --local-b 10.2.10.11
"""
import argparse
import json
import selectors
import socket
import struct
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import moldudp64 as mold

parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True, help="this server's name in metrics, e.g. sub1")
parser.add_argument("--local-a", required=True, help="this server's address on the A network")
parser.add_argument("--local-b", required=True, help="this server's address on the B network")
parser.add_argument("--rcvbuf", type=int, default=4 * 1024 * 1024, help="SO_RCVBUF per feed socket")
parser.add_argument("--gap-timeout", type=float, default=0.15,
                    help="seconds to wait for the other feed before re-requesting; keep above the measured A/B skew (lab max ~100 ms)")
parser.add_argument("--retry-interval", type=float, default=0.3)
parser.add_argument("--max-retries", type=int, default=3)
parser.add_argument("--retransmit-port", type=int, default=40000)
parser.add_argument("--metrics-port", type=int, default=9108)
parser.add_argument("--stats", default="/tmp/handler-stats.json")
args = parser.parse_args()

FEED_NAMES = list(mold.FEEDS)  # ["A", "B"]
LOCAL = {"A": args.local_a, "B": args.local_b}
REQUEST = struct.Struct(">10sQH")  # MoldUDP64 re-request: session, first sequence wanted, count
lock = threading.Lock()  # the metrics thread reads the same state the main loop writes

# ---------------------------------------------------------------- state

# What each feed delivered on its own.
feed = {f: {"packets": 0, "messages": 0, "heartbeats": 0, "gaps": 0, "gap_messages": 0,
            "wins": 0, "duplicates": 0, "last_packet": 0.0} for f in FEED_NAMES}

# Per channel, after merging A and B.
#   next     next sequence number we expect
#   pending  ranges that arrived ahead of `next`, {start: end}; non-empty means a gap is open
#   fresh    nothing received yet (so start from whatever arrives instead of asking for history)
#   ended    end-of-session seen (the next sequence 1 starts a new replay pass)
chan = [{"next": 1, "pending": {}, "gap_since": None, "retries": 0, "last_request": 0.0,
         "fresh": True, "ended": False, "feed_next": {f: None for f in FEED_NAMES}}
        for _ in range(mold.CHANNELS)]

merged = {"messages": 0, "gaps": 0, "retransmit_requests": 0, "recovered": 0, "unrecovered": 0, "sessions": 0}

# A/B skew: how much later the second copy of each packet arrives than the first.
first_seen = {}  # (channel, first seq) -> (feed that won, arrival time)
skews = deque(maxlen=200_000)


def reset_channel(ch):
    c = chan[ch]
    c.update(next=1, pending={}, gap_since=None, retries=0, last_request=0.0, fresh=False, ended=False)
    c["feed_next"] = {f: None for f in FEED_NAMES}
    for key in [k for k in first_seen if k[0] == ch]:
        del first_seen[key]


# ---------------------------------------------------------------- A/B arbitration

def drain(c, now):
    """Deliver buffered ranges that have become contiguous; close the gap once nothing is waiting."""
    while True:
        ready = [s for s in c["pending"] if s <= c["next"]]
        if not ready:
            break
        for s in ready:
            e = c["pending"].pop(s)
            if e > c["next"]:
                merged["messages"] += e - c["next"]
                c["next"] = e
    c["gap_since"] = now if c["pending"] else None  # a later hole may still be open
    c["retries"] = 0


def accept(ch, start, end, source, now):
    """Merge the sequence range [start, end) into channel ch. source is "A", "B" or "R" (retransmission)."""
    c = chan[ch]
    if end <= c["next"]:  # already delivered: this is the other feed's copy
        if source in feed:
            feed[source]["duplicates"] += 1
            note_second_copy(ch, start, source, now)
        return
    if start <= c["next"]:  # the range we were waiting for (or overlapping it)
        gained = end - c["next"]
        merged["messages"] += gained
        if source in feed:
            feed[source]["wins"] += 1
            note_first_copy(ch, start, source, now)
        else:
            merged["recovered"] += gained
        c["next"] = end
        drain(c, now)
        return
    # ahead of what we expect: something before it is missing. Buffer it and open a gap.
    if start not in c["pending"] or c["pending"][start] < end:
        c["pending"][start] = end
        if source in feed:
            feed[source]["wins"] += 1
            note_first_copy(ch, start, source, now)
    elif source in feed:
        feed[source]["duplicates"] += 1
        note_second_copy(ch, start, source, now)
    if c["gap_since"] is None:
        c["gap_since"] = now


def note_first_copy(ch, start, source, now):
    first_seen[(ch, start)] = (source, now)


def note_second_copy(ch, start, source, now):
    seen = first_seen.pop((ch, start), None)
    if seen and seen[0] != source:
        skews.append(now - seen[1])


# ---------------------------------------------------------------- gap recovery

def check_gaps(now, request_sock):
    """Re-request ranges that both feeds have been missing for longer than --gap-timeout."""
    for ch, c in enumerate(chan):
        if c["gap_since"] is None or now - c["gap_since"] < args.gap_timeout:
            continue
        if now - c["last_request"] < args.retry_interval:
            continue
        missing_to = min(c["pending"])
        if c["retries"] == 0:
            merged["gaps"] += 1  # the other feed didn't fill it in time: missing on both
        if c["retries"] >= args.max_retries:
            # give up: count the hole as lost and carry on from the buffered data
            merged["unrecovered"] += missing_to - c["next"]
            c["next"] = missing_to
            drain(c, now)
            continue
        count = min(missing_to - c["next"], 0xFFFE)
        req = REQUEST.pack(mold.session_name(ch), c["next"], count)
        side = FEED_NAMES[c["retries"] % 2]  # ask the A-side server first, then alternate
        request_sock[side].sendto(req, (mold.FEEDS[side][0], args.retransmit_port))
        merged["retransmit_requests"] += 1
        c["retries"] += 1
        c["last_request"] = now


# ---------------------------------------------------------------- packet handling

def on_feed_packet(source, ch, seq, count, now):
    st, c = feed[source], chan[ch]
    st["last_packet"] = now

    if count in (mold.HEARTBEAT, mold.END_OF_SESSION):
        # Both carry the next sequence number. If it's ahead of us, the last packets were lost
        # on both feeds and nothing after them would ever reveal the hole, so open a gap now.
        if not c["fresh"] and not c["ended"] and seq > c["next"] and seq not in c["pending"]:
            c["pending"][seq] = seq
            if c["gap_since"] is None:
                c["gap_since"] = now
        if count == mold.HEARTBEAT:
            st["heartbeats"] += 1
        else:
            c["ended"] = True
        return

    if c["fresh"]:  # joined mid-stream: start from what is arriving now
        c["next"], c["fresh"] = seq, False
        if ch == 0:
            merged["sessions"] += 1
    elif seq == 1 and c["ended"]:  # a new replay pass restarts sequence numbers at 1
        reset_channel(ch)
        if ch == 0:
            merged["sessions"] += 1

    st["packets"] += 1
    st["messages"] += count
    expected = c["feed_next"][source]
    if expected is not None and seq > expected:  # this feed on its own skipped some messages
        st["gaps"] += 1
        st["gap_messages"] += seq - expected
    c["feed_next"][source] = max(expected or 0, seq + count)
    accept(ch, seq, seq + count, source, now)


# ---------------------------------------------------------------- metrics and stats

def udp_rcvbuf_errors():
    """Kernel UDP drops because a socket receive buffer was full (RcvbufErrors in /proc/net/snmp)."""
    try:
        lines = [l.split() for l in open("/proc/net/snmp") if l.startswith("Udp:")]
        return int(lines[1][lines[0].index("RcvbufErrors")])
    except (OSError, ValueError, IndexError):
        return 0


def skew_quantiles():
    if not skews:
        return {}
    s = sorted(skews)
    pick = lambda q: round(s[min(len(s) - 1, int(q * len(s)))] * 1000, 2)
    return {"samples": len(s), "p50_ms": pick(0.5), "p99_ms": pick(0.99), "p999_ms": pick(0.999),
            "max_ms": round(s[-1] * 1000, 2)}


# name -> (type, help). Every metric carries a host label; per-feed ones also carry feed.
METRIC_HELP = {
    "tradehunt_feed_packets_total": ("counter", "data packets received per feed"),
    "tradehunt_feed_messages_total": ("counter", "messages received per feed"),
    "tradehunt_feed_heartbeats_total": ("counter", "MoldUDP64 heartbeats received per feed"),
    "tradehunt_feed_gaps_total": ("counter", "sequence jumps seen on this feed alone"),
    "tradehunt_feed_gap_messages_total": ("counter", "messages this feed alone was missing"),
    "tradehunt_arbitration_wins_total": ("counter", "ranges this feed delivered first"),
    "tradehunt_feed_duplicates_total": ("counter", "ranges already delivered by the other feed"),
    "tradehunt_feed_last_packet_age_seconds": ("gauge", "seconds since this feed last delivered a packet (-1 = never)"),
    "tradehunt_merged_messages_total": ("counter", "messages delivered after the A/B merge"),
    "tradehunt_merged_gaps_total": ("counter", "gaps missing on both feeds"),
    "tradehunt_retransmit_requests_total": ("counter", "MoldUDP64 re-requests sent"),
    "tradehunt_recovered_messages_total": ("counter", "messages recovered by retransmission"),
    "tradehunt_unrecovered_messages_total": ("counter", "messages given up on"),
    "tradehunt_sessions_total": ("counter", "MoldUDP64 sessions started (replay passes)"),
    "tradehunt_udp_rcvbuf_errors_total": ("counter", "kernel UDP drops: socket receive buffer full"),
    "tradehunt_ab_skew_ms": ("gauge", "how much later the second feed copy arrives (recent samples)"),
    "tradehunt_open_gaps": ("gauge", "channels currently waiting on missing messages"),
}


def metric_samples():
    """(name, labels, value) for every metric, read under the lock."""
    now = time.time()
    host = f'host="{args.name}"'
    out = []
    for f in FEED_NAMES:
        st, lbl = feed[f], f'{host},feed="{f}"'
        out += [("tradehunt_feed_packets_total", lbl, st["packets"]),
                ("tradehunt_feed_messages_total", lbl, st["messages"]),
                ("tradehunt_feed_heartbeats_total", lbl, st["heartbeats"]),
                ("tradehunt_feed_gaps_total", lbl, st["gaps"]),
                ("tradehunt_feed_gap_messages_total", lbl, st["gap_messages"]),
                ("tradehunt_arbitration_wins_total", lbl, st["wins"]),
                ("tradehunt_feed_duplicates_total", lbl, st["duplicates"]),
                ("tradehunt_feed_last_packet_age_seconds", lbl,
                 round(now - st["last_packet"], 3) if st["last_packet"] else -1)]
    out += [("tradehunt_merged_messages_total", host, merged["messages"]),
            ("tradehunt_merged_gaps_total", host, merged["gaps"]),
            ("tradehunt_retransmit_requests_total", host, merged["retransmit_requests"]),
            ("tradehunt_recovered_messages_total", host, merged["recovered"]),
            ("tradehunt_unrecovered_messages_total", host, merged["unrecovered"]),
            ("tradehunt_sessions_total", host, merged["sessions"]),
            ("tradehunt_udp_rcvbuf_errors_total", host, udp_rcvbuf_errors()),
            ("tradehunt_open_gaps", host, sum(1 for c in chan if c["gap_since"] is not None))]
    for q, v in skew_quantiles().items():
        if q.endswith("_ms"):
            out.append(("tradehunt_ab_skew_ms", f'{host},quantile="{q[:-3]}"', v))
    return out


def metrics_text():
    with lock:
        samples = metric_samples()
    lines, described = [], set()
    for name, labels, value in samples:
        if name not in described:
            kind, text = METRIC_HELP[name]
            lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
            described.add(name)
        lines.append(f"{name}{{{labels}}} {value}")
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = metrics_text().encode()
        self.send_response(200 if self.path.startswith("/metrics") else 404)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def write_stats():
    with lock:
        snapshot = {"feeds": {f: {k: v for k, v in feed[f].items() if k != "last_packet"} for f in FEED_NAMES},
                    "merged": dict(merged), "ab_skew": skew_quantiles(), "udp_rcvbuf_errors": udp_rcvbuf_errors(),
                    "next_seq": [c["next"] for c in chan]}
    json.dump(snapshot, open(args.stats, "w"), indent=2)


# ---------------------------------------------------------------- main

sel = selectors.DefaultSelector()

# one multicast socket per (feed, channel), joined to (source, group) on that feed's interface
for f, (source, _) in mold.FEEDS.items():
    for ch in range(mold.CHANNELS):
        grp = mold.group(f, ch)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.rcvbuf)
        s.bind((grp, mold.port(ch)))
        mreq = socket.inet_aton(grp) + socket.inet_aton(LOCAL[f]) + socket.inet_aton(source)
        s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_ADD_SOURCE_MEMBERSHIP", 39), mreq)
        s.setblocking(False)
        sel.register(s, selectors.EVENT_READ, (f, ch))

# one unicast socket per side for re-requests and their replies
request_sock = {}
for f in FEED_NAMES:
    r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r.bind((LOCAL[f], 0))
    r.setblocking(False)
    request_sock[f] = r
    sel.register(r, selectors.EVENT_READ, ("R", None))

threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", args.metrics_port), MetricsHandler).serve_forever,
                 daemon=True).start()
print(f"{args.name}: handler running, metrics on :{args.metrics_port}", flush=True)

buf = bytearray(2048)
last_stats = 0.0
while True:
    events = sel.select(timeout=0.01)
    now = time.time()
    with lock:
        for key, _ in events:
            source, ch = key.data
            while True:
                try:
                    n = key.fileobj.recv_into(buf)
                except BlockingIOError:
                    break
                if n < mold.HEADER_LEN:
                    continue
                session, seq, count = mold.HEADER.unpack_from(buf)
                if source == "R":  # a retransmission reply; the session name says which channel
                    if count not in (mold.HEARTBEAT, mold.END_OF_SESSION):
                        accept(int(session[-2:]) - 1, seq, seq + count, "R", now)
                else:
                    on_feed_packet(source, ch, seq, count, now)
        check_gaps(now, request_sock)
    if now - last_stats > 1:
        with lock:
            for key in [k for k, (_, t0) in first_seen.items() if now - t0 > 5]:
                del first_seen[key]  # the other feed never sent this one
        write_stats()
        last_stats = now
