"""Tests for tools/feedscope.py.

Synthetic tests build captures packet by packet, so every expected number is known exactly.
The real-data tests replay the Nasdaq slice through tools/mkcapture.py with seeded faults and check
that feedscope reports exactly what was injected; they are skipped when data/ is not present.

    python3 -m unittest discover -s tests -v
"""
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "feed"))
import feedscope  # noqa: E402
import mkcapture  # noqa: E402
import moldudp64 as mold  # noqa: E402

DATA = os.path.join(ROOT, "data", "itch50_05_15_open_slice.bin")
SIDES = [feedscope.parse_side("A=232.1.1.0/24"), feedscope.parse_side("B=232.2.1.0/24")]
MS = 1_000_000
BASE = 1_700_000_000 * 1_000_000_000


def mold_pkt(seq, count, session=b"TESTSESS01"):
    block = b"".join(b"\x00\x0c" + bytes(12) for _ in range(count))  # 12-byte dummy messages
    return mold.pack(session, seq, block, count)


def write_pcapng(path, records):
    """Minimal pcapng writer: one section, one Ethernet interface with nanosecond timestamps."""
    def block(btype, body):
        body += bytes(-len(body) % 4)
        n = 12 + len(body)
        return struct.pack("<II", btype, n) + body + struct.pack("<I", n)
    out = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += block(1, struct.pack("<HHI", 1, 0, 65535) + struct.pack("<HHB3x", 9, 1, 9) + struct.pack("<HH", 0, 0))
    for ts, fr in records:
        out += block(6, struct.pack("<IIIII", 0, ts >> 32, ts & 0xFFFFFFFF, len(fr), len(fr)) + fr)
    with open(path, "wb") as f:
        f.write(out)


class Synthetic(unittest.TestCase):
    """Two sides, one session, 100 packets of 3 messages every 10 ms. Faults placed by hand."""

    def build(self, a_skip=(), b_skip=(), a_stop_after=None, b_delay_ms=0.0, end_marker=True):
        recs = []
        for i in range(100):
            seq = 1 + 3 * i
            t = BASE + i * 10 * MS
            if i not in a_skip and (a_stop_after is None or i <= a_stop_after):
                recs.append((t, mkcapture.frame("10.1.0.10", "232.1.1.1", 30001, mold_pkt(seq, 3))))
            if i not in b_skip:
                recs.append((t + int(b_delay_ms * MS), mkcapture.frame("10.2.0.10", "232.2.1.1", 30001, mold_pkt(seq, 3))))
        if end_marker:
            t = BASE + 100 * 10 * MS
            for src, grp in (("10.1.0.10", "232.1.1.1"), ("10.2.0.10", "232.2.1.1")):
                if src == "10.1.0.10" and a_stop_after is not None:
                    continue
                recs.append((t, mkcapture.frame(src, grp, 30001, mold.pack(b"TESTSESS01", 301, b"", mold.END_OF_SESSION))))
        return sorted(recs)

    def run_capture(self, recs, fmt="pcap", silence_s=0.1):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t." + fmt)
            (mkcapture.write_pcap if fmt == "pcap" else write_pcapng)(path, recs)
            return feedscope.analyze(path, SIDES, int(silence_s * 1e9), None, feedscope.DEFAULT_TRUESIZE, 3)

    def test_clean(self):
        r = self.run_capture(self.build())
        s = r["sessions"][0]
        for side in "AB":
            self.assertEqual(s["sides"][side]["messages"], 300)
            self.assertEqual(s["sides"][side]["gap_events"], 0)
        self.assertEqual(s["missing_on_all_sides"]["messages"], 0)
        self.assertEqual(r["silences"], [])

    def test_gaps_covered_by_other_side(self):
        r = self.run_capture(self.build(a_skip={10, 11, 50}, b_skip={70}))
        s = r["sessions"][0]
        self.assertEqual((s["sides"]["A"]["gap_events"], s["sides"]["A"]["gap_messages"]), (2, 9))
        self.assertEqual((s["sides"]["B"]["gap_events"], s["sides"]["B"]["gap_messages"]), (1, 3))
        self.assertEqual(s["missing_on_all_sides"]["messages"], 0)

    def test_capture_that_joins_mid_stream(self):
        # drop the first 30 packets on both sides: the capture "starts" at seq 91, which is not a gap
        recs = [x for x in self.build() if not any(mold_pkt(1 + 3 * i, 3)[10:18] in x[1] for i in range(30))]
        r = self.run_capture(recs)
        s = r["sessions"][0]
        self.assertEqual(s["first_seq"], 91)
        self.assertEqual(s["missing_on_all_sides"]["messages"], 0)
        self.assertEqual(s["sides"]["A"]["gap_messages"], 0)

    def test_reordering_is_not_loss(self):
        recs = self.build()
        # swap the arrival times of B packets 30 and 31
        b = [i for i, (t, f) in enumerate(recs) if f[30:33] == bytes([232, 2, 1])]
        i, j = b[30], b[31]
        recs[i], recs[j] = (recs[i][0], recs[j][1]), (recs[j][0], recs[i][1])
        r = self.run_capture(recs)
        side_b = r["sessions"][0]["sides"]["B"]
        self.assertEqual((side_b["gap_messages"], side_b["gap_events"], side_b["late"]), (0, 0, 1))

    def test_loss_on_both_sides(self):
        r = self.run_capture(self.build(a_skip={20, 21, 60}, b_skip={21, 60, 61}))
        m = r["sessions"][0]["missing_on_all_sides"]
        self.assertEqual((m["messages"], m["holes"]), (6, 2))  # packets 21 and 60 lost on both

    def test_tail_loss_seen_through_end_of_session(self):
        r = self.run_capture(self.build(a_skip={99}, b_skip={99}))
        s = r["sessions"][0]
        self.assertEqual(s["sides"]["A"]["gap_messages"], 3)
        self.assertEqual(s["missing_on_all_sides"]["messages"], 3)

    def test_silent_side_hidden_by_arbitration(self):
        r = self.run_capture(self.build(a_stop_after=39))  # A dies after 0.39 s
        a = [x for x in r["silences"] if x["side"] == "A"]
        self.assertEqual(len(a), 1)
        self.assertAlmostEqual(a[0]["start_s"], 0.39, places=3)
        self.assertTrue(a[0]["other_side_active"])
        self.assertTrue(a[0]["to_end_of_capture"])
        self.assertEqual(r["sessions"][0]["missing_on_all_sides"]["messages"], 0)

    def test_side_never_seen(self):
        recs = [x for x in self.build() if b"\xe8\x01\x01" not in x[1][30:34]]  # drop every 232.1.1.x frame
        r = self.run_capture(recs)
        self.assertTrue(any(x.get("never_seen") and x["side"] == "A" for x in r["silences"]))

    def test_ab_skew_and_first_arrival(self):
        r = self.run_capture(self.build(b_delay_ms=0.5))
        s = r["sessions"][0]
        self.assertEqual(s["ab_skew_ms"]["p50"], 0.5)
        self.assertEqual(s["ab_skew_ms"]["max"], 0.5)
        self.assertEqual(s["first_arrival_share"]["A"], 1.0)

    def test_pcapng_matches_pcap(self):
        recs = self.build(a_skip={5}, b_delay_ms=0.25)
        a, b = self.run_capture(recs, "pcap"), self.run_capture(recs, "pcapng")
        self.assertEqual(a["sessions"], b["sessions"])
        self.assertEqual(a["bursts"], b["bursts"])

    def test_burst_peaks(self):
        # 40 packets inside 505-506 ms, a millisecond with no steady-stream packet: the 1 ms peak must be exactly 40
        recs = self.build()
        for k in range(40):
            recs.append((BASE + 505 * MS + k * 20_000, mkcapture.frame("10.1.0.10", "232.1.1.2", 30002, mold_pkt(1 + k, 1, b"TESTSESS02"))))
        r = self.run_capture(sorted(recs))
        one_ms = next(x for x in r["bursts"]["A"]["rates"] if x["window"] == "1 ms")
        self.assertEqual(one_ms["peak_pps"], 40_000)
        self.assertEqual(r["bursts"]["A"]["worst_1ms"][0], {"at_s": 0.505, "packets": 40})


@unittest.skipUnless(os.path.exists(DATA), "Nasdaq data slice not present in data/")
class RealData(unittest.TestCase):
    """Real Nasdaq messages 09:29:58-09:30:01 at real speed, with seeded faults."""

    def run_case(self, **faults):
        recs, inj = mkcapture.build(DATA, mkcapture.hms_to_ns("09:29:58"), mkcapture.hms_to_ns("09:30:01"), 1.0,
                                    faults.get("loss", {}), faults.get("delay", {}), faults.get("jitter", {}),
                                    faults.get("kill", {}), seed=7)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "real.pcap")
            mkcapture.write_pcap(path, recs)
            return feedscope.analyze(path, SIDES, int(faults.get("silence", 2.0) * 1e9), None,
                                     feedscope.DEFAULT_TRUESIZE, 3), inj

    def test_loss_counts_match_exactly(self):
        r, inj = self.run_case(loss={"A": 0.02, "B": 0.02}, jitter={"B": 0.3})
        for side in "AB":
            found = sum(s["sides"][side]["gap_messages"] for s in r["sessions"])
            self.assertEqual(found, inj["dropped"][side]["messages"], side)
        both = {(ch, seq, n) for (ch, seq, n) in inj["dropped_keys"]["A"] & inj["dropped_keys"]["B"]}
        self.assertEqual(sum(s["missing_on_all_sides"]["messages"] for s in r["sessions"]), sum(n for _, _, n in both))
        self.assertEqual(sum(s["missing_on_all_sides"]["holes"] for s in r["sessions"]), len(both))

    def test_injected_delay_is_measured(self):
        r, _ = self.run_case(delay={"B": 0.4}, jitter={"B": 0.3})
        for s in r["sessions"]:  # jitter reorders B's packets; that must not be counted as loss
            self.assertEqual(s["sides"]["B"]["gap_messages"], 0)
            self.assertEqual(s["missing_on_all_sides"]["messages"], 0)
        self.assertGreater(sum(s["sides"]["B"]["late"] for s in r["sessions"]), 0)
        for s in r["sessions"]:
            self.assertGreaterEqual(s["ab_skew_ms"]["p50"], 0.4)
            self.assertLessEqual(s["ab_skew_ms"]["max"], 0.7)
            self.assertEqual(s["first_arrival_share"]["A"], 1.0)

    def test_killed_side_is_reported_silent(self):
        r, _ = self.run_case(kill={"A": (1.5, None)}, silence=0.5)
        a = [x for x in r["silences"] if x["side"] == "A"]
        self.assertEqual(len(a), 1)
        self.assertAlmostEqual(a[0]["start_s"], 1.5, delta=0.01)
        self.assertTrue(a[0]["other_side_active"])
        self.assertEqual(sum(s["missing_on_all_sides"]["messages"] for s in r["sessions"]), 0)


if __name__ == "__main__":
    unittest.main()
