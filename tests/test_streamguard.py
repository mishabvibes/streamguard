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
from streamguard import links as L  # noqa: E402


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


class Links(unittest.TestCase):
    def link(self, dev, kind, metric, fails=0, healthy=True, up=None, rtt=30):
        l = L.Link(dev, kind, gateway="10.0.0.1", metric=metric)
        l.fails, l.healthy, l.up_mbps, l.rtt_ms = fails, healthy, up, rtt
        return l

    def test_no_failover_while_primary_ok(self):
        links = [self.link("wlo1", "wifi", 600, fails=2), self.link("enp2s0", "ethernet", 700)]
        self.assertIsNone(L.choose_failover(links, "wlo1"))

    def test_failover_prefers_wired_then_fastest(self):
        links = [self.link("wlo1", "wifi", 100, fails=3, healthy=False),
                 self.link("usb0", "usb-tether", 700, up=40),
                 self.link("enp2s0", "ethernet", 800, up=5)]
        self.assertEqual(L.choose_failover(links, "wlo1"), "enp2s0")
        links[2].healthy = False
        self.assertEqual(L.choose_failover(links, "wlo1"), "usb0")

    def test_no_failover_without_healthy_backup(self):
        links = [self.link("wlo1", "wifi", 100, fails=5, healthy=False),
                 self.link("usb0", "usb-tether", 700, healthy=False)]
        self.assertIsNone(L.choose_failover(links, "wlo1"))

    def test_safe_bitrate(self):
        self.assertIsNone(L.safe_bitrate_mbps([self.link("a", "wifi", 1)]))
        self.assertEqual(L.safe_bitrate_mbps([self.link("a", "wifi", 1, up=10),
                                              self.link("b", "ethernet", 2, up=20)]), 14.0)

    def test_default_routes_json(self):
        text = ('[{"dst":"default","gateway":"10.0.0.1","dev":"wlo1","metric":600},'
                '{"dst":"default","gateway":"192.168.42.129","dev":"usb0","metric":100,'
                '"prefsrc":"192.168.42.10"}]')
        r = L.default_routes(text)
        self.assertEqual([x["dev"] for x in r], ["wlo1", "usb0"])
        self.assertEqual(r[1]["metric"], 100)
        self.assertEqual(L.default_routes("garbage"), [])

    def test_split_is_additive_and_restore_reapplies(self):
        calls = []
        orig = L.run
        L.run = lambda cmd, timeout=10: (calls.append(cmd), (0, "[]", ""))[1]
        try:
            m = L.LinkManager(lambda d: "ethernet", lambda: None, lambda *a: None)
            m.links = {"enp2s0": self.link("enp2s0", "ethernet", 100)}
            m.apply_split({"youtube": "enp2s0"}, lambda k: ["1.1.1.1", "2.2.2.2"])
            m.apply_split({"youtube": "enp2s0"}, lambda k: ["2.2.2.2", "3.3.3.3"])
            adds = [c for c in calls if c[:3] == ["nmcli", "device", "modify"]]
            self.assertEqual(adds[0][4:], ["+ipv4.routes", "1.1.1.1/32 10.0.0.1",
                                           "+ipv4.routes", "2.2.2.2/32 10.0.0.1"])
            self.assertEqual(adds[1][4:], ["+ipv4.routes", "3.3.3.3/32 10.0.0.1"])
            self.assertEqual(len(m.split_routes["enp2s0"]), 3)   # 1.1.1.1 kept
            self.assertEqual(m.links["enp2s0"].platforms, ["youtube"])
            m.restore()
            self.assertIn(["nmcli", "device", "reapply", "enp2s0"], calls)
            self.assertEqual(m.split_routes, {})
        finally:
            L.run = orig

    def test_speed_parse(self):
        self.assertEqual(L._mbps("200 1250000"), 10.0)
        self.assertEqual(L._mbps("000 625000.5"), 5.0)   # stopped by --max-time
        self.assertEqual(L._mbps("100 561674"), 4.5)     # upload cut off mid-body
        self.assertIsNone(L._mbps("403 7"))
        self.assertIsNone(L._mbps(""))

    def test_network_checks(self):
        c = Checklist.snap(self, failover=True, links_healthy=1, planned_mbps=8.2, safe_mbps=6.0,
                           obs_reconnect=False)
        st = {x.title: x.status for x in c}
        self.assertEqual(st["Backup connection"], WARN)
        self.assertEqual(st["Upload capacity"], WARN)
        self.assertEqual(st["Auto-reconnect"], WARN)
        c = Checklist.snap(self, failover=True, links_healthy=2, planned_mbps=6, safe_mbps=14)
        st = {x.title: x.status for x in c}
        self.assertEqual(st["Backup connection"], OK)
        self.assertEqual(st["Upload capacity"], OK)


if __name__ == "__main__":
    unittest.main()
