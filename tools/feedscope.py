#!/usr/bin/env python3
"""feedscope: feed quality and microburst report for any MoldUDP64 packet capture.

Reads a pcap or pcapng file (from tcpdump, Wireshark, a SPAN/TAP port or a cloud packet mirror)
and reports, per feed side (A/B) and per MoldUDP64 session:

  quality   gaps, duplicates, heartbeats; A/B skew and which side arrives first; messages missing
            on BOTH sides (only retransmission can recover them); a side that goes silent while
            the other keeps sending, which A/B arbitration hides from the client
  bursts    peak rates at 1 s, 100 ms, 10 ms, 1 ms and 100 us; the worst 1 ms bursts; and, per
            socket (one multicast group), the receive buffer a consumer needs to absorb them

Standard library only.

    python3 feedscope.py capture.pcap
    python3 feedscope.py capture.pcapng --side A=232.1.1.0/24 --side B=232.2.1.0/24 --json
    python3 feedscope.py capture.pcap --drain-pps 20000      # size buffers for a known consumer rate
"""
import argparse
import ipaddress
import json
import math
import re
import struct
import sys
from collections import defaultdict

MOLD_HEADER = struct.Struct(">10sQH")
HEARTBEAT, END_OF_SESSION = 0, 0xFFFF
WINDOWS_NS = [(1_000_000_000, "1 s"), (100_000_000, "100 ms"), (10_000_000, "10 ms"),
              (1_000_000, "1 ms"), (100_000, "100 us")]
# Linux charges each queued packet its skb "truesize", not its length. For <=1.5 KB UDP packets on
# common drivers that is about 2.3 KB; it varies by NIC driver, so treat the estimate as approximate.
DEFAULT_TRUESIZE = 2304


# ---------------------------------------------------------------- capture reading

def read_capture(path):
    """Yield (ts_ns, wire_len, frame bytes, linktype) for every packet in a pcap or pcapng file."""
    with open(path, "rb") as f:
        data = f.read()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        yield from _read_pcap(data)
    elif magic == b"\x0a\x0d\x0d\x0a":
        yield from _read_pcapng(data)
    else:
        raise SystemExit(f"{path}: not a pcap or pcapng file")


def _read_pcap(data):
    end = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    nano = data[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack(end + "I", data[20:24])[0] & 0x0FFFFFFF
    rec = struct.Struct(end + "IIII")
    off = 24
    while off + 16 <= len(data):
        sec, frac, incl, orig = rec.unpack_from(data, off)
        off += 16
        yield sec * 1_000_000_000 + (frac if nano else frac * 1000), orig, data[off:off + incl], linktype
        off += incl


def _read_pcapng(data):
    off, end, ifaces = 0, "<", []
    while off + 12 <= len(data):
        btype = struct.unpack_from(end + "I", data, off)[0]
        if btype == 0x0A0D0D0A:  # section header: byte order can change per section
            end = "<" if data[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        blen = struct.unpack_from(end + "I", data, off + 4)[0]
        if blen < 12:
            break
        body = data[off + 8:off + blen - 4]
        if btype == 1:  # interface description: link type and timestamp resolution
            linktype = struct.unpack_from(end + "H", body, 0)[0]
            res, o = ("dec", 6), 8  # default: microsecond ticks
            while o + 4 <= len(body):
                code, olen = struct.unpack_from(end + "HH", body, o)
                if code == 0:
                    break
                if code == 9 and olen >= 1:
                    v = body[o + 4]
                    res = ("bin" if v & 0x80 else "dec", v & 0x7F)
                o += 4 + olen + (-olen % 4)
            ifaces.append((linktype, res))
        elif btype == 6:  # enhanced packet
            iface, hi, lo, incl, orig = struct.unpack_from(end + "IIIII", body, 0)
            linktype, res = ifaces[iface]
            yield _ticks_to_ns((hi << 32) | lo, res), orig, body[20:20 + incl], linktype
        off += blen


def _ticks_to_ns(ticks, res):
    """Exact integer conversion of a pcapng timestamp: 10^-v or 2^-v seconds per tick."""
    kind, v = res
    if kind == "dec":
        return ticks * 10 ** (9 - v) if v <= 9 else ticks // 10 ** (v - 9)
    return ticks * 1_000_000_000 >> v


def udp_payload(frame, linktype):
    """Return (src, dst, dport, payload) for an unfragmented IPv4/UDP frame, else None."""
    if linktype == 1:  # Ethernet, with optional VLAN tags
        o, etype = 14, int.from_bytes(frame[12:14], "big")
        while etype in (0x8100, 0x88A8) and len(frame) >= o + 4:
            etype, o = int.from_bytes(frame[o + 2:o + 4], "big"), o + 4
    elif linktype == 113:  # Linux cooked capture (tcpdump -i any)
        o, etype = 16, int.from_bytes(frame[14:16], "big")
    elif linktype == 276:  # Linux cooked capture v2
        o, etype = 20, int.from_bytes(frame[0:2], "big")
    elif linktype in (101, 12, 228):  # raw IPv4
        o, etype = 0, 0x0800
    else:
        return None
    if etype != 0x0800 or len(frame) < o + 28 or frame[o] >> 4 != 4 or frame[o + 9] != 17:
        return None
    if int.from_bytes(frame[o + 6:o + 8], "big") & 0x3FFF:  # fragment
        return None
    ihl = (frame[o] & 0x0F) * 4
    u = o + ihl
    ulen = int.from_bytes(frame[u + 4:u + 6], "big")
    return frame[o + 12:o + 16], frame[o + 16:o + 20], int.from_bytes(frame[u + 2:u + 4], "big"), frame[u + 8:u + ulen]


# ---------------------------------------------------------------- analysis

class Coverage:
    """Which sequence numbers a side has seen, as a growable bitmap."""
    def __init__(self):
        self.bits = bytearray()

    def mark(self, first, count):
        need = first + count
        if need > len(self.bits):
            self.bits.extend(bytes(need - len(self.bits) + 65536))
        b = self.bits
        dup = 0
        for s in range(first, first + count):
            dup += b[s]
            b[s] = 1
        return dup


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def analyze(path, sides, silence_ns, drain_pps, truesize, top):
    side_of = lambda dst: next((n for n, net in sides if dst in net), None)
    first_ts = last_ts = None
    unmatched = 0
    # per side
    pkts, wire = defaultdict(int), defaultdict(int)
    times = defaultdict(list)                       # side -> packet timestamps
    # per (side, session)
    st = defaultdict(lambda: {"packets": 0, "messages": 0, "heartbeats": 0, "eos": 0, "late": 0, "duplicates": 0, "next": None, "max_seq": 0, "min_seq": None, "cov": Coverage()})
    # per socket (side, group, port): arrival times and frame lengths for burst/buffer analysis
    sock = defaultdict(list)
    copies = {}                                     # (session, first seq) -> (side, ts)
    skew = defaultdict(list)                        # session -> skew ns
    wins = defaultdict(lambda: defaultdict(int))    # session -> side -> first arrivals

    for ts, orig, frame, lt in read_capture(path):
        p = udp_payload(frame, lt)
        if p is None or len(p[3]) < MOLD_HEADER.size:
            unmatched += 1
            continue
        src, dst, dport, pay = p
        side = side_of(ipaddress.IPv4Address(dst))
        if side is None:
            unmatched += 1
            continue
        first_ts = ts if first_ts is None else min(first_ts, ts)
        last_ts = ts if last_ts is None else max(last_ts, ts)
        session, seq, count = MOLD_HEADER.unpack_from(pay)
        session = session.decode("ascii", "replace").strip()
        pkts[side] += 1
        wire[side] += orig
        times[side].append(ts)
        sock[(side, str(ipaddress.IPv4Address(dst)), dport)].append((ts, orig))
        s = st[(side, session)]
        s["packets"] += 1
        if count in (HEARTBEAT, END_OF_SESSION):
            s["heartbeats" if count == HEARTBEAT else "eos"] += 1
            s["max_seq"] = max(s["max_seq"], seq - 1)  # a heartbeat says everything before seq was sent
            s["min_seq"] = seq if s["min_seq"] is None else min(s["min_seq"], seq)
            continue
        s["messages"] += count
        if s["next"] is not None and seq < s["next"]:
            s["late"] += 1  # arrived after a later packet: reordered (or a duplicate, counted below)
        s["duplicates"] += s["cov"].mark(seq, count)
        s["next"] = max(s["next"] or 0, seq + count)
        s["max_seq"] = max(s["max_seq"], seq + count - 1)
        s["min_seq"] = seq if s["min_seq"] is None else min(s["min_seq"], seq)
        key = (session, seq)
        other = copies.pop(key, None)
        if other is None:
            copies[key] = (side, ts)
        elif other[0] != side:
            skew[session].append(ts - other[1])
            wins[session]["tie" if ts == other[1] else other[0]] += 1

    if first_ts is None:
        raise SystemExit("no MoldUDP64 packets matched the --side networks")
    span = max(1, last_ts - first_ts)
    side_names = [n for n, _ in sides if pkts[n]]

    # ---- quality
    sessions = sorted({sess for (_, sess) in st})
    q_sessions = []
    for sess in sessions:
        # Gaps are counted at the end, from what actually arrived, so reordered packets are not gaps.
        # Counting starts at the first sequence number any side showed: a capture usually joins the
        # stream mid-session, and what was sent before it started is not missing. Each side is judged
        # up to the highest sequence it has evidence for (data or heartbeat), so a side that dies is
        # reported as silent, not as missing everything after it died.
        per_side, covs = {}, []
        here = [st[(n, sess)] for n in side_names if (n, sess) in st]
        top_seq = max(s["max_seq"] for s in here)
        first_seq = min(s["min_seq"] for s in here if s["min_seq"] is not None)
        for n in side_names:
            s = st.get((n, sess))
            if not s:
                continue
            seen = _window(s["cov"].bits, first_seq, s["max_seq"])
            per_side[n] = {k: s[k] for k in ("packets", "messages", "heartbeats", "eos", "late", "duplicates")}
            per_side[n]["gap_messages"] = seen.count(0)
            per_side[n]["gap_events"] = _runs(seen)
            covs.append(_window(s["cov"].bits, first_seq, top_seq))
        # messages missing on every side, between the first and highest sequence any side showed
        width = max(0, top_seq - first_seq + 1)
        union = 0
        for c in covs:
            union |= int.from_bytes(c, "big")
        union = union.to_bytes(width, "big")
        both_missing, holes = union.count(0), _runs(union)
        sk = sorted(skew[sess])
        q_sessions.append({
            "session": sess, "sides": per_side, "first_seq": first_seq, "highest_seq": top_seq,
            "missing_on_all_sides": {"messages": both_missing, "holes": holes},
            "ab_skew_ms": None if not sk else {k: round(quantile(sk, v) / 1e6, 3) for k, v in
                                               (("p50", .5), ("p99", .99), ("p999", .999), ("max", 1.0))},
            "first_arrival_share": {n: round(wins[sess][n] / len(sk), 3) for n in side_names + ["tie"]} if sk else None,
        })

    # silence: intervals where one side sent nothing for >= silence_ns while another side was active
    silent = []
    for n in side_names:
        others = sorted(t for m in side_names if m != n for t in times[m])
        tl = [first_ts] + times[n] + [last_ts]
        for a, b in zip(tl, tl[1:]):
            if b - a >= silence_ns:
                lo = _bisect(others, a)
                active = lo < len(others) and others[lo] < b
                silent.append({"side": n, "start_s": round((a - first_ts) / 1e9, 3),
                               "duration_s": round((b - a) / 1e9, 3), "other_side_active": active,
                               "to_end_of_capture": b == last_ts})
    for n, _ in sides:
        if not pkts[n]:
            silent.append({"side": n, "start_s": 0.0, "duration_s": round(span / 1e9, 3),
                           "other_side_active": True, "to_end_of_capture": True, "never_seen": True})

    # ---- bursts
    bursts = {}
    for n in side_names:
        rows = []
        for w, label in WINDOWS_NS:
            if w > span and w != WINDOWS_NS[0][0]:
                continue
            cnt, byt = defaultdict(int), defaultdict(int)
            for (sn, _, _), arr in sock.items():
                if sn != n:
                    continue
                for t, l in arr:
                    k = (t - first_ts) // w
                    cnt[k] += 1
                    byt[k] += l
            pk = max(cnt.values())
            rows.append({"window": label, "peak_pps": round(pk * 1e9 / w), "peak_mbps": round(max(byt.values()) * 8e3 / w, 1)})
        avg = pkts[n] * 1e9 / span
        one_ms = next(r for r in rows if r["window"] == "1 ms") if any(r["window"] == "1 ms" for r in rows) else None
        # worst 1 ms windows
        cnt = defaultdict(int)
        for (sn, _, _), arr in sock.items():
            if sn == n:
                for t, _ in arr:
                    cnt[(t - first_ts) // 1_000_000] += 1
        worst = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        bursts[n] = {"packets": pkts[n], "average_pps": round(avg), "rates": rows,
                     "burst_factor_1ms_vs_avg": round(one_ms["peak_pps"] / avg, 1) if one_ms and avg else None,
                     "worst_1ms": [{"at_s": round(k / 1e3, 3), "packets": c} for k, c in worst]}

    # ---- buffer sizing: the busiest socket (one multicast group) per side, read by a consumer that
    # drains at a steady rate. Rates are multiples of that socket's busiest 1-second rate, i.e. what
    # sizing on per-second averages would assume, plus --drain-pps if given.
    buffers = []
    for n in side_names:
        per_sock = {}
        for (sn, grp, port), arr in sock.items():
            if sn == n:
                arr.sort()
                per_s = defaultdict(int)
                for t, _ in arr:
                    per_s[(t - first_ts) // 1_000_000_000] += 1
                per_sock[f"{grp}:{port}"] = (max(per_s.values()), arr)
        name, (busy, arr) = max(per_sock.items(), key=lambda kv: kv[1][0])
        rates = [(f"{m}x busiest second", busy * m) for m in (1, 2, 5, 10)]
        if drain_pps:
            rates.append(("--drain-pps", drain_pps))
        rows = []
        for label, rate in rates:
            backlog, peak, prev, peak_at = 0.0, 0.0, arr[0][0], arr[0][0]
            for t, _ in arr:
                backlog = max(0.0, backlog - (t - prev) * rate / 1e9) + 1
                prev = t
                if backlog > peak:
                    peak, peak_at = backlog, t
            need = math.ceil(peak) * truesize
            rows.append({"consumer": label, "drain_pps": rate, "peak_backlog_packets": math.ceil(peak),
                         "peak_at_s": round((peak_at - first_ts) / 1e9, 3), "kernel_bytes_needed": need,
                         "request_so_rcvbuf": _round_up(need)})
        buffers.append({"side": n, "busiest_socket": name, "busiest_second_pps": busy, "rows": rows})

    return {"capture": path, "duration_s": round(span / 1e9, 3), "non_feed_packets": unmatched,
            "sides": {n: str(net) for n, net in sides}, "sessions": q_sessions, "silences": silent,
            "bursts": bursts, "buffers": buffers,
            "buffer_model": {"truesize_bytes_per_packet": truesize, "consumer": "steady reader at the listed rates"}}


def _window(bits, lo, hi):
    """Bitmap bytes for sequence numbers lo..hi (0 = not seen)."""
    width = max(0, hi - lo + 1)
    w = bytes(bits[lo:hi + 1])
    return w + bytes(width - len(w))


def _runs(b):
    """Number of runs of zero bytes, i.e. separate holes."""
    return len(re.findall(b"\x00+", b))


def _bisect(arr, x):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _round_up(n):
    """Round up to a power of two, at least 256 KB, the way buffer sizes are usually set."""
    return max(256 * 1024, 1 << max(0, math.ceil(math.log2(max(1, n)))))


def _kb(n):
    return f"{n / 1024 / 1024:.0f} MB" if n >= 1024 * 1024 else f"{math.ceil(n / 1024)} KB"


# ---------------------------------------------------------------- report

def report(r):
    out = [f"feedscope: {r['capture']}  ({r['duration_s']} s, sides "
           + ", ".join(f"{k}={v}" for k, v in r["sides"].items()) + ")", "=" * 72, "FEED QUALITY"]
    for s in r["sessions"]:
        out.append(f"  session {s['session']}  (seq {s['first_seq']:,} to {s['highest_seq']:,})")
        for n, v in s["sides"].items():
            out.append(f"    {n}: {v['messages']:,} msgs in {v['packets']:,} pkts · gaps {v['gap_events']} "
                       f"({v['gap_messages']:,} msgs) · late {v['late']:,} · dups {v['duplicates']:,} · heartbeats {v['heartbeats']}")
        m = s["missing_on_all_sides"]
        flag = "  <-- needs retransmission" if m["messages"] else ""
        out.append(f"    missing on all sides: {m['messages']:,} msgs in {m['holes']} holes{flag}")
        if s["ab_skew_ms"]:
            k = s["ab_skew_ms"]
            share = ", ".join(f"{n} {v:.0%}" for n, v in s["first_arrival_share"].items() if n != "tie" or v)
            out.append(f"    A/B skew ms: p50 {k['p50']} · p99 {k['p99']} · p99.9 {k['p999']} · max {k['max']} · first: {share}")
    out.append("SILENCES")
    if not r["silences"]:
        out.append("  none: every side kept sending")
    for x in r["silences"]:
        what = "never seen in the capture" if x.get("never_seen") else (
            f"silent {x['duration_s']} s from t={x['start_s']} s" + (" to end of capture" if x["to_end_of_capture"] else ""))
        hidden = "  <-- other side kept sending: arbitration hides this from clients" if x["other_side_active"] else "  (all sides quiet)"
        out.append(f"  side {x['side']}: {what}{hidden}")
    out.append("MICROBURSTS")
    for n, b in r["bursts"].items():
        rates = " · ".join(f"{x['window']} {x['peak_pps']:,}" for x in b["rates"])
        out.append(f"  side {n}: average {b['average_pps']:,} pps · peak pps per window: {rates}")
        if b["burst_factor_1ms_vs_avg"]:
            out.append(f"    busiest 1 ms is {b['burst_factor_1ms_vs_avg']}x the average rate; worst 1 ms windows: "
                       + ", ".join(f"t={w['at_s']} s ({w['packets']} pkts)" for w in b["worst_1ms"]))
    out.append(f"RECEIVE BUFFERS (busiest socket per side; ~{r['buffer_model']['truesize_bytes_per_packet']} B kernel charge per queued packet)")
    for x in r["buffers"]:
        out.append(f"  side {x['side']} {x['busiest_socket']} (busiest second {x['busiest_second_pps']:,} pps):")
        for y in x["rows"]:
            out.append(f"    consumer reads {y['drain_pps']:>8,} pps ({y['consumer']:<18}) peak backlog {y['peak_backlog_packets']:>5,} pkts"
                       f" at t={y['peak_at_s']} s · needs ~{_kb(y['kernel_bytes_needed'])} · request SO_RCVBUF >= {_kb(y['request_so_rcvbuf'])}")
    out.append("  (Linux doubles the SO_RCVBUF you request, leaving 2x headroom, and caps it at net.core.rmem_max: raise that too.\n"
               "   A consumer that stalls, e.g. on a busy CPU, needs more: these are minimums for a steady reader.)")
    if r["non_feed_packets"]:
        out.append(f"({r['non_feed_packets']:,} packets were not MoldUDP64 on the given sides and were ignored)")
    return "\n".join(out)


def parse_side(text):
    name, _, net = text.partition("=")
    if not name or not net:
        raise argparse.ArgumentTypeError("use NAME=CIDR, e.g. A=232.1.1.0/24")
    return name, ipaddress.IPv4Network(net, strict=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture")
    ap.add_argument("--side", action="append", type=parse_side,
                    help="feed side as NAME=destination CIDR (default: TradeHunt's A=232.1.1.0/24, B=232.2.1.0/24)")
    ap.add_argument("--silence", type=float, default=2.0, help="seconds without packets that count as a silent side (default 2)")
    ap.add_argument("--drain-pps", type=int, help="consumer read rate per socket for buffer sizing (default: busiest second)")
    ap.add_argument("--truesize", type=int, default=DEFAULT_TRUESIZE, help="kernel bytes charged per queued packet")
    ap.add_argument("--top", type=int, default=3, help="worst 1 ms windows to list per side")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    sides = a.side or [parse_side("A=232.1.1.0/24"), parse_side("B=232.2.1.0/24")]
    r = analyze(a.capture, sides, int(a.silence * 1e9), a.drain_pps, a.truesize, a.top)
    print(json.dumps(r, indent=2) if a.json else report(r))


if __name__ == "__main__":
    sys.exit(main())
