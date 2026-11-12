"""Parser checks against modem pages saved from the SB8200 (fw AC01.01.008_122722_8200.03.07.733)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import modem_check as m  # noqa: E402


def fx(fun):
    return (HERE / "fixtures" / f"fun{fun}.xml").read_text()


class Parsers(unittest.TestCase):
    def test_global(self):
        g = m.parse_global(fx(1))
        self.assertEqual(g["hw_model"], "SB8200v3")
        self.assertEqual(g["uptime_s"], 3 * 3600 + 44 * 60 + 40)
        self.assertEqual(g["modem_time"].hour, 18)

    def test_downstream(self):
        ds = m.parse_downstream(fx(10), fx(19), fx(9))
        self.assertEqual(sum(r["kind"] == "SC-QAM" for r in ds), 32)
        ch32 = next(r for r in ds if r["chid"] == 32)
        self.assertEqual((ch32["freq_hz"], ch32["corrected"], ch32["uncorrectable"]), (651000000, 318, 51))
        ofdm = next(r for r in ds if r["chid"] == 193)
        self.assertEqual((ofdm["kind"], ofdm["uncorrectable"]), ("OFDM", 1154))

    def test_upstream(self):
        us = m.parse_upstream(fx(11), fx(6))
        self.assertEqual(sorted(r["chid"] for r in us), [1, 2, 3, 4])  # OFDMA channels are disabled
        self.assertEqual(next(r for r in us if r["chid"] == 1)["power_dbmv"], 43.0)

    def test_eventlog(self):
        ev = m.parse_eventlog(fx(13))
        first = ev[0]
        self.assertEqual(first["time"].isoformat(), "2026-01-15T14:51:26")  # DD/MM/YYYY
        self.assertTrue(any("T3 time-out" in e["text"] for e in ev))
        self.assertEqual(m.short_event_text(ev[1]["text"]), "No Ranging Response received - T3 time-out")

    def test_uptime_days(self):
        self.assertEqual(m.parse_uptime("2day(s)1h:00m:05s"), 2 * 86400 + 3600 + 5)


if __name__ == "__main__":
    unittest.main()
