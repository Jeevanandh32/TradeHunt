"""MoldUDP64 retransmission server for the exchange.

A request is a 20-byte packet: session (10), first sequence number wanted (8), message count (2).
The reply is an ordinary MoldUDP64 packet holding as many of those messages as fit in one packet;
the client asks again for anything still missing. The server builds its message store with the same
prepare() call as the publisher, so it must be started with the same --from/--to window.

    python3 retransmitter.py /data/itch50_05_15_open_slice.bin --from 09:29:58 --to 09:30:05
"""
import argparse
import socket
import struct

import moldudp64 as mold
from itchreplay import hms_to_ns, prepare

REQUEST = struct.Struct(">10sQH")

parser = argparse.ArgumentParser()
parser.add_argument("file")
parser.add_argument("--from", dest="start", default="09:29:30")
parser.add_argument("--to", dest="end", default="09:31:00")
parser.add_argument("--port", type=int, default=40000)
args = parser.parse_args()

_, total, channel_messages = prepare(args.file, hms_to_ns(args.start), hms_to_ns(args.end))
by_session = {mold.session_name(ch): channel_messages[ch] for ch in range(mold.CHANNELS)}
print(f"retransmitter ready: {total:,} messages on UDP {args.port}", flush=True)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", args.port))
budget = mold.MAX_PACKET - mold.HEADER_LEN
served = 0
while True:
    data, client = s.recvfrom(64)
    if len(data) < REQUEST.size:
        continue
    session, seq, count = REQUEST.unpack_from(data)
    messages = by_session.get(session)
    if messages is None or seq < 1 or seq > len(messages):
        continue
    block, n, size = [], 0, 0
    for m in messages[seq - 1:seq - 1 + count]:
        if size + len(m) > budget:
            break
        block.append(m)
        n += 1
        size += len(m)
    s.sendto(mold.pack(session, seq, b"".join(block), n), client)
    served += n
