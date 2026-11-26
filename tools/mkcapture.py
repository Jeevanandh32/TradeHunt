#!/usr/bin/env python3
"""mkcapture: write a pcap of TradeHunt's A/B feeds from the real Nasdaq data, with known faults.

Packets come from the same plan the publisher uses (feed/itchreplay.py), so sequence numbers and
packet boundaries match the lab. Each packet is stamped with the exchange time of its last message,
scaled by --speed, so the capture keeps the real sub-millisecond burst shape of the market.
Faults are seeded and exact, which is what makes it useful for testing feedscope: every gap, delay
and silence in the output is one that was put there on purpose.

    python3 mkcapture.py /data/itch50_05_15_open_slice.bin open.pcap --from 09:29:58 --to 09:30:05
    python3 mkcapture.py DATA out.pcap --loss A=0.02 --loss B=0.02 --delay B=0.4 --jitter B=0.3 --kill A=3:2
"""
import argparse
import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "feed"))
import moldudp64 as mold  # noqa: E402
from itchreplay import hms_to_ns, prepare  # noqa: E402

HEARTBEAT_INTERVAL_NS = 1_000_000_000


def last_message_ns(block):
    off, ts = 0, None
    while off + 2 <= len(block):
        length = int.from_bytes(block[off:off + 2], "big")
        ts = int.from_bytes(block[off + 7:off + 13], "big")
        off += 2 + length
    return ts


def ip_checksum(hdr):
    s = sum(struct.unpack("!10H", hdr))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def frame(src, grp, dport, payload):
    g = bytes(int(x) for x in grp.split("."))
    dst_mac = bytes([0x01, 0x00, 0x5E, g[1] & 0x7F, g[2], g[3]])
    eth = dst_mac + bytes.fromhex("02000a000001") + b"\x08\x00"
    udp = struct.pack("!HHHH", 40000, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 32, 17, 0,
                     bytes(int(x) for x in src.split(".")), g)
    ip = ip[:10] + struct.pack("!H", ip_checksum(ip)) + ip[12:]
    return eth + ip + udp


def kv(text, conv=float):
    k, _, v = text.partition("=")
    if k not in mold.FEEDS or not v:
        raise argparse.ArgumentTypeError(f"use SIDE=VALUE with SIDE in {sorted(mold.FEEDS)}")
    return k, conv(v)


def kill_window(text):
    start, _, dur = text.partition(":")
    return float(start), (float(dur) if dur else None)


def build(data, start_ns, end_ns, speed, loss, delay_ms, jitter_ms, kill, seed):
    """Return a time-sorted list of (ts_ns, frame) and a dict of what was injected."""
    rnd = random.Random(seed)
    packets, total, _ = prepare(data, start_ns, end_ns)
    t0 = packets[0][0] * 1_000_000
    base = 1_700_000_000 * 1_000_000_000  # arbitrary capture epoch
    out, injected = [], {"messages": total, "packets_per_side": len(packets),
                         "dropped": {f: {"packets": 0, "messages": 0} for f in mold.FEEDS},
                         "killed": {}, "dropped_keys": {f: set() for f in mold.FEEDS}}
    sessions = [mold.session_name(ch) for ch in range(mold.CHANNELS)]
    next_seq = [1] * mold.CHANNELS
    last_wall = [None] * mold.CHANNELS

    def emit(wall, ch, payload, count):
        for f, (src, _) in mold.FEEDS.items():
            rel = (wall - base) / 1e9
            if f in kill:
                ks, kd = kill[f]
                if rel >= ks and (kd is None or rel < ks + kd):
                    continue
            if count and f in loss and rnd.random() < loss[f]:
                injected["dropped"][f]["packets"] += 1
                injected["dropped"][f]["messages"] += count
                injected["dropped_keys"][f].add((ch, int.from_bytes(payload[10:18], "big"), count))
                continue
            t = wall + int(delay_ms.get(f, 0) * 1e6) + int(rnd.random() * jitter_ms.get(f, 0) * 1e6)
            out.append((t, frame(src, mold.group(f, ch), mold.port(ch), payload)))

    for ms, ch, seq, count, block in packets:
        msg_ns = last_message_ns(block) or ms * 1_000_000
        wall = base + int((msg_ns - t0) / speed)
        # heartbeat any channel that has been quiet for a second of capture time
        for c in range(mold.CHANNELS):
            while last_wall[c] is not None and wall - last_wall[c] >= HEARTBEAT_INTERVAL_NS:
                hb = last_wall[c] + HEARTBEAT_INTERVAL_NS
                emit(hb, c, mold.pack(sessions[c], next_seq[c], b"", mold.HEARTBEAT), 0)
                last_wall[c] = hb
        emit(wall, ch, mold.pack(sessions[ch], seq, block, count), count)
        next_seq[ch] = seq + count
        last_wall[ch] = wall
    end_wall = max(w for w in last_wall if w is not None)
    for c in range(mold.CHANNELS):  # close every session, so tail losses are visible
        emit(end_wall + 1_000_000, c, mold.pack(sessions[c], next_seq[c], b"", mold.END_OF_SESSION), 0)
    for f, (ks, kd) in kill.items():
        injected["killed"][f] = {"from_s": ks, "for_s": kd}
    out.sort(key=lambda x: x[0])
    return out, injected


def write_pcap(path, records):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))  # nanosecond pcap, Ethernet
        for ts, fr in records:
            f.write(struct.pack("<IIII", ts // 1_000_000_000, ts % 1_000_000_000, len(fr), len(fr)))
            f.write(fr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data", help="raw ITCH 5.0 file")
    ap.add_argument("out", help="pcap to write")
    ap.add_argument("--from", dest="start", default="09:29:58")
    ap.add_argument("--to", dest="end", default="09:30:05")
    ap.add_argument("--speed", type=float, default=1.0, help="1.0 = real exchange time")
    ap.add_argument("--loss", action="append", type=kv, default=[], help="SIDE=probability per packet")
    ap.add_argument("--delay", action="append", type=kv, default=[], help="SIDE=fixed extra delay in ms")
    ap.add_argument("--jitter", action="append", type=kv, default=[], help="SIDE=uniform extra delay 0..ms")
    ap.add_argument("--kill", action="append", type=lambda t: kv(t, kill_window), default=[],
                    help="SIDE=START[:DURATION] seconds into the capture; no duration = until the end")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    records, injected = build(a.data, hms_to_ns(a.start), hms_to_ns(a.end), a.speed, dict(a.loss),
                              dict(a.delay), dict(a.jitter), dict(a.kill), a.seed)
    write_pcap(a.out, records)
    injected["records"] = len(records)
    injected.pop("dropped_keys")
    print(injected)


if __name__ == "__main__":
    sys.exit(main())
