"""Turn a Nasdaq TotalView-ITCH 5.0 file into TradeHunt's MoldUDP64 packet plan.

The input is the raw ITCH format ([2-byte length][message] ...). Messages are split across
channels by stock locate and batched per millisecond of exchange time. The publisher and the
retransmission server both call prepare(), so sequence numbers always agree.
"""
from collections import defaultdict

import moldudp64 as mold

NS_PER_MS = 1_000_000


def hms_to_ns(text):
    h, m, s = (int(x) for x in text.split(":"))
    return ((h * 60 + m) * 60 + s) * 1_000_000_000


def prepare(path, start_ns, end_ns):
    """Return (packets, total, channel_messages).

    packets: time-ordered (exchange ms, channel, first seq, count, block)
    channel_messages: per channel, the length-prefixed messages in sequence order (seq n at index n-1)
    """
    buckets = defaultdict(list)  # (ms, channel) -> [length-prefixed message, ...]
    total = 0
    with open(path, "rb") as f:
        data = f.read()
    off, end = 0, len(data)
    while off + 2 <= end:
        length = int.from_bytes(data[off:off + 2], "big")
        msg_end = off + 2 + length
        if msg_end > end or length < 11:
            break
        ts = int.from_bytes(data[off + 7:off + 13], "big")  # timestamp at message offset 5
        if start_ns <= ts < end_ns:
            locate = int.from_bytes(data[off + 3:off + 5], "big")
            buckets[(ts // NS_PER_MS, locate % mold.CHANNELS)].append(data[off:msg_end])
            total += 1
        off = msg_end

    next_seq = [1] * mold.CHANNELS
    channel_messages = [[] for _ in range(mold.CHANNELS)]
    packets = []
    budget = mold.MAX_PACKET - mold.HEADER_LEN
    for (ms, ch) in sorted(buckets):
        block, count, size = [], 0, 0
        for m in buckets[(ms, ch)]:
            if size + len(m) > budget and count:
                packets.append((ms, ch, next_seq[ch], count, b"".join(block)))
                next_seq[ch] += count
                block, count, size = [], 0, 0
            block.append(m)
            count += 1
            size += len(m)
            channel_messages[ch].append(m)
        if count:
            packets.append((ms, ch, next_seq[ch], count, b"".join(block)))
            next_seq[ch] += count
    return packets, total, channel_messages
