import base64
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from streamguard import rules  # noqa: E402
from streamguard.config import Config  # noqa: E402
from streamguard.obs import Output, auth_string, is_stream  # noqa: E402
from streamguard.rules import FAIL, NA, OK, WARN, Snapshot, checklist, overall  # noqa: E402
from streamguard.system import default_iface, wifi_dbm  # noqa: E402


class Detectors(unittest.TestCase):
    def test_silence_fires_once_and_restores(self):
        d = rules.SilenceDetector(-50, hold=8)
        self.assertIsNone(d.feed(-20, 0))
        self.assertIsNone(d.feed(-70, 1))
        self.assertIsNone(d.feed(-70, 8.9))
        self.assertEqual(d.feed(-70, 9.0), "silent")
        self.assertIsNone(d.feed(-70, 20))
        self.assertEqual(d.feed(-10, 21), "restored")
        self.assertIsNone(d.feed(-10, 22))

    def test_short_gap_is_not_silence(self):
        d = rules.SilenceDetector(-50, hold=8)
        for t in range(0, 30, 4):
            d.feed(-70, t)
            self.assertIsNone(d.feed(-10, t + 0.5))

    def test_clip_needs_repeats_and_cools_down(self):
        c = rules.ClipDetector(-0.5, count=3, window=5, cooldown=30)
        self.assertFalse(c.feed(0, 0))
        self.assertFalse(c.feed(0, 1))
        self.assertTrue(c.feed(0, 2))
        for t in (3, 4, 5):
            self.assertFalse(c.feed(0, t))   # cooldown
        self.assertFalse(c.feed(-0.1, 40))
        self.assertFalse(c.feed(-0.1, 41))
        self.assertTrue(c.feed(-0.1, 42))

    def test_rate_tracker(self):
        r = rules.RateTracker(window=30)
        r.add(0, bytes=0, skipped=0, total=0)
        r.add(10, bytes=10_000_000, skipped=30, total=600)
        self.assertAlmostEqual(r.rate("bytes"), 1_000_000)
        self.assertAlmostEqual(r.ratio("skipped", "total"), 0.05)
        r.add(11, bytes=5, skipped=0, total=0)   # OBS restarted: counters reset
        self.assertIsNone(r.rate("bytes"))


class Checklist(unittest.TestCase):
    def snap(self, **kw):
        s = Snapshot(on_ac=True, battery=90, profile="performance", net_kind="ethernet",
                     link_mbps=1000, ingest_ms={"youtube": 40}, obs_state="connected",
                     recording_free_gb=200, audio_device="Mixer", audio_present=True,
                     audio_peak_db=-12, sample_rate=48000, video_expected=["Cam"],
                     video_present=["Cam"], cpu_temp=60, gpu_temp=55)
        for k, v in kw.items():
            setattr(s, k, v)
        return checklist(s, {"youtube": "YouTube"})

    def status(self, checks, title):
        return next(c.status for c in checks if c.title == title)

    def test_all_good(self):
        self.assertEqual(overall(self.snap()), OK)

    def test_failures(self):
        c = self.snap(on_ac=False, audio_present=False, video_present=[],
                      ingest_ms={"youtube": None})
        self.assertEqual(self.status(c, "Charger"), FAIL)
        self.assertEqual(self.status(c, "Audio input"), FAIL)
        self.assertEqual(self.status(c, "Cam"), FAIL)
        self.assertEqual(self.status(c, "YouTube"), FAIL)
        self.assertEqual(overall(c), FAIL)

    def test_warnings(self):
        c = self.snap(net_kind="wifi", wifi_dbm=-66, sample_rate=44100, audio_peak_db=-80,
                      profile="balance", obs_state="disabled")
        for t in ("Connection", "Sample rate", "Signal", "Performance mode", "OBS link"):
            self.assertEqual(self.status(c, t), WARN, t)

    def test_weak_wifi_fails(self):
        self.assertEqual(self.status(self.snap(net_kind="wifi", wifi_dbm=-78), "Connection"), FAIL)

    def test_no_lpowerd_is_not_an_error(self):
        self.assertEqual(self.status(self.snap(profile=None), "Performance mode"), NA)


class OBS(unittest.TestCase):
    def test_auth_matches_protocol(self):
        # obs-websocket v5: base64(sha256(base64(sha256(pw + salt)) + challenge))
        pw, salt, chal = "supersecret", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=", \
            "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="
        sec = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
        want = base64.b64encode(hashlib.sha256((sec + chal).encode()).digest()).decode()
        self.assertEqual(auth_string(pw, salt, chal), want)

    def test_output_kinds(self):
        self.assertTrue(is_stream(Output("simple_stream", "rtmp_output")))
        self.assertTrue(is_stream(Output("srt", "ffmpeg_mpegts_muxer")))
        self.assertTrue(is_stream(Output("YouTube (Aitum)", "rtmp_output")))
        self.assertFalse(is_stream(Output("simple_file_output", "ffmpeg_muxer")))
        self.assertFalse(is_stream(Output("adv_file_output", "mp4_output")))


class Parsing(unittest.TestCase):
    def test_default_route_lowest_metric(self):
        route = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                 "wlo1\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n"
                 "enp2s0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n"
                 "wlo1\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n")
        self.assertEqual(default_iface(route), "enp2s0")
        self.assertIsNone(default_iface("Iface\tDestination\n"))

    def test_wifi_dbm(self):
        text = ("Inter-| sta-|   Quality        |   Discarded packets\n"
                " face | tus | link level noise |  nwid  crypt   frag\n"
                "  wlo1: 0000   64.  -46.  -256        0      0      0\n")
        self.assertEqual(wifi_dbm("wlo1", text), -46.0)
        self.assertIsNone(wifi_dbm("wlan9", text))

    def test_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c", "config.json")
            c = Config(p)
            c.set(audio_device="Yamaha MG10XU", bogus=1)
            c2 = Config(p)
            self.assertEqual(c2["audio_device"], "Yamaha MG10XU")
            self.assertNotIn("bogus", c2.data)
            self.assertFalse(c2["alert_sound"])


if __name__ == "__main__":
    unittest.main()
