"""Exchange publisher: replay real Nasdaq ITCH 5.0 data as MoldUDP64 on feeds A and B.

Every packet is sent twice, once per feed (A from 10.1.0.10, B from 10.2.0.10), at a chosen
speed relative to exchange time (1.0 = real time). Packets are prepared before the clock starts,
so the replay loop only waits and sends. Quiet channels get MoldUDP64 heartbeats.

    python3 publisher.py /data/itch50_05_15_open_slice.bin --speed 0.1 --from 09:29:58 --to 09:30:05
    python3 publisher.py /data/itch50_05_15_open_slice.bin --speed 0.1 --loop    # run continuously
"""
import argparse
import json
import socket
import time
from collections import defaultdict

import moldudp64 as mold
from itchreplay import hms_to_ns, prepare

HEARTBEAT_INTERVAL = 1.0  # seconds a channel may stay quiet before it gets a heartbeat


def feed_socket(source, ttl):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    s.bind((source, 0))
    return s


def replay_once(packets, speed, socks, sessions):
    """Send every packet on both feeds, paced by exchange time. Returns per-second stats."""
    per_second = defaultdict(lambda: [0, 0, 0.0])  # exchange second -> [messages, packets, worst lag ms]
    first_ms = packets[0][0]
    start = time.monotonic()
    next_seq = [1] * mold.CHANNELS
    last_sent = [start] * mold.CHANNELS

    def send(ch, pkt):
        for f, sock in socks.items():
            sock.sendto(pkt, (mold.group(f, ch), mold.port(ch)))

    for ms, ch, seq, count, block in packets:
        target = start + (ms - first_ms) / 1000 / speed
        while target - time.monotonic() > 0.0005:
            now = time.monotonic()
            for c in range(mold.CHANNELS):
                if now - last_sent[c] >= HEARTBEAT_INTERVAL:
                    send(c, mold.pack(sessions[c], next_seq[c], b"", mold.HEARTBEAT))
                    last_sent[c] = now
            time.sleep(min(target - now, HEARTBEAT_INTERVAL / 2))
        lag_ms = max(0.0, (time.monotonic() - target) * 1000)

        send(ch, mold.pack(sessions[ch], seq, block, count))
        next_seq[ch] = seq + count
        last_sent[ch] = time.monotonic()
        sec = per_second[ms // 1000]
        sec[0] += count
        sec[1] += 1
        sec[2] = max(sec[2], lag_ms)

    # tell subscribers the stream finished; the sequence number lets them spot a lost tail
    for ch in range(mold.CHANNELS):
        send(ch, mold.pack(sessions[ch], next_seq[ch], b"", mold.END_OF_SESSION))
    return per_second, time.monotonic() - start


parser = argparse.ArgumentParser()
parser.add_argument("file")
parser.add_argument("--from", dest="start", default="09:29:30", help="exchange time to start from")
parser.add_argument("--to", dest="end", default="09:31:00", help="exchange time to stop at")
parser.add_argument("--speed", type=float, default=0.1, help="1.0 = real time (the lab handles about 0.1 at the open)")
parser.add_argument("--loop", action="store_true", help="replay the window again and again")
parser.add_argument("--ttl", type=int, default=16, help="multicast TTL (incident 1 sets this to 1)")
parser.add_argument("--stats", default="/tmp/publisher-stats.json")
args = parser.parse_args()

t0 = time.time()
packets, total, _ = prepare(args.file, hms_to_ns(args.start), hms_to_ns(args.end))
print(f"prepared {total:,} messages in {len(packets):,} packets per feed ({time.time() - t0:.1f}s)", flush=True)

socks = {f: feed_socket(source, args.ttl) for f, (source, _) in mold.FEEDS.items()}
sessions = [mold.session_name(ch) for ch in range(mold.CHANNELS)]

passes = 0
while True:
    per_second, elapsed = replay_once(packets, args.speed, socks, sessions)
    passes += 1
    peak = max(per_second, key=lambda s: per_second[s][0])
    stats = {
        "messages": total,
        "packets_per_feed": len(packets),
        "speed": args.speed,
        "exchange_window": f"{args.start}-{args.end}",
        "pass": passes,
        "wall_seconds": round(elapsed, 2),
        "msgs_per_sec_avg": round(total / elapsed),
        "peak_exchange_second": time.strftime("%H:%M:%S", time.gmtime(peak)),
        "peak_second_msgs": per_second[peak][0],
        "peak_second_packets_per_feed": per_second[peak][1],
        "worst_lag_ms": round(max(v[2] for v in per_second.values()), 1),
    }
    json.dump(stats, open(args.stats, "w"), indent=2)
    print(json.dumps(stats, indent=2), flush=True)
    if not args.loop:
        break
    time.sleep(2)
