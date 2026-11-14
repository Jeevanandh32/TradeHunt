"""MoldUDP64 framing and TradeHunt's feed/channel plan, shared by every feed program.

A MoldUDP64 packet is a 20-byte header followed by length-prefixed messages:
    session (10 bytes) | sequence number of the first message (8) | message count (2)
A count of 0 is a heartbeat; 0xFFFF marks the end of a session.
"""
import struct

HEADER = struct.Struct(">10sQH")
HEADER_LEN = HEADER.size
HEARTBEAT = 0
END_OF_SESSION = 0xFFFF
MAX_PACKET = 1400  # stay well under a 1500-byte MTU

# Symbols are split across channels; each channel is its own multicast group and MoldUDP64 session.
CHANNELS = 4
PORT_BASE = 30001

# feed name -> (source address, multicast group prefix). Channel n uses group prefix + (n + 1).
FEEDS = {
    "A": ("10.1.0.10", "232.1.1."),
    "B": ("10.2.0.10", "232.2.1."),
}


def session_name(channel):
    return f"TH0515CH{channel + 1:02d}".encode()


def group(feed, channel):
    return FEEDS[feed][1] + str(channel + 1)


def port(channel):
    return PORT_BASE + channel


def pack(session, seq, block, count):
    """block is a run of length-prefixed messages (empty for a heartbeat or end of session)."""
    return HEADER.pack(session, seq, count) + block
