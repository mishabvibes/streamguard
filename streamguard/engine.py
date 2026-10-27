"""Engine: owns the monitors, turns readings into alerts, runs live sessions.

Nothing runs unless the app is open. Before going live the app is a
checklist (alerts show in the window only). Once live, alerts also go out as
desktop notifications, optional sound and phone pushes.
"""

import json
import logging
import os
import time
from datetime import datetime

from gi.repository import GLib

from . import alerts as A
from .alerts import Alerts
from .config import INGESTS
from .devices import DeviceWatcher, LevelMeter
from .obs import OBSClient, is_stream, recording_path
from .rules import ClipDetector, SilenceDetector, Snapshot, checklist
from .system import (CpuLoad, Gpu, IngestProber, Lpowerd, Network, SleepInhibitor, cpu_temp,
                     disk_free_gb, power_state, sample_rate)

log = logging.getLogger(__name__)

TICK_S = 2
PROBE_S = 15
SLOW_S = 10


def fmt_bps(bps):
    if bps is None:
        return "—"
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} Mb/s"
    return f"{bps / 1e3:.0f} kb/s"


class LiveSession:
    def __init__(self):
        self.started = time.time()
        self.t0 = time.monotonic()
        self.samples = []
        self.prev_profile = None
        self.max = {"cpu_temp": None, "gpu_temp": None}
        self.min_dbm = None
        self.peak_out_kbps = {}
        self.out_bytes = {}

    @property
    def elapsed(self):
        return time.monotonic() - self.t0


class Engine:
    def __init__(self, config, on_update):
        self.config = config
        self.on_update = on_update
        self.alerts = Alerts(config, self._changed)
        self.alerts.muted = True
        self.devices = DeviceWatcher(self._on_device)
        self.meter = LevelMeter(self._on_level, self._on_meter_error)
        self.net = Network()
        self.prober = IngestProber(lambda k, ms: self._changed())
        self.cpu = CpuLoad()
        self.gpu = Gpu()
        self.obs = OBSClient(self._changed, self._on_obs_event,
                             config["obs_port"], config["obs_password"])
        self.lpowerd = Lpowerd()
        self.inhibitor = SleepInhibitor()
        self._make_detectors()
        self.peak = [-120.0, -120.0]
        self.rms = [-120.0, -120.0]
        self._peak_hist = []
        self.meter_error = None
        self.values = {}
        self.live = None
        self._sources = []
        self._prev_net_kind = None
        self._prev_outputs = {}
        self._prev_stats = {}
        self.on_level_update = None   # GUI hook: called ~10x/s with fresh levels
        self.running = False

    def _make_detectors(self):
        self.silence = SilenceDetector(self.config["silence_db"], self.config["silence_seconds"])
        self.clip = ClipDetector(self.config["clip_db"])

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.running:
            return
        self.running = True
        self.devices.start()
        self.gpu.start()
        self.obs.start()
        self._tick()
        self._slow()
        self._probe()
        self._sources = [GLib.timeout_add_seconds(TICK_S, self._tick),
                         GLib.timeout_add_seconds(SLOW_S, self._slow),
                         GLib.timeout_add_seconds(PROBE_S, self._probe)]

    def stop(self):
        if self.live:
            self.end_live()
        for s in self._sources:
            GLib.source_remove(s)
        self._sources = []
        self.meter.stop()
        self.gpu.stop()
        self.obs.stop()
        self.devices.stop()
        self.running = False

    def reconfigure(self):
        """Settings changed: rebuild detectors, re-pick the metered device."""
        self._make_detectors()
        self._pick_meter_device()
        self._probe()
        self._changed()

    def _changed(self):
        self.on_update()

    # -------------------------------------------------------------- devices
    def _pick_meter_device(self):
        want = self.config["audio_device"]
        dev = self.devices.devices.get(("audio", want)) if want else None
        if dev is None:
            self.meter.stop()
            self.meter.device_name = None
            self.peak = [-120.0, -120.0]
            return
        if self.meter.device_name != want or self.meter.pipeline is None:
            self.meter_error = None
            self.meter.start(dev)

    def _on_device(self, what, key):
        self._pick_meter_device()
        if what and key:
            kind, name = key
            pinned = (name == self.config["audio_device"] if kind == "audio"
                      else name in self.config["video_devices"])
            if pinned:
                akey = f"device:{kind}:{name}"
                label = "Audio input" if kind == "audio" else "Capture device"
                if what == "removed":
                    self.alerts.raise_(akey, A.CRITICAL, f"{label} disconnected",
                                       f"{name} was unplugged or lost power.")
                else:
                    self.alerts.clear(akey, f"{name} is back")
        self._changed()

    def _on_level(self, peak, rms):
        self.peak, self.rms = peak, rms
        top = max(peak) if peak else -120.0
        now = time.monotonic()
        self._peak_hist.append((now, top))
        while self._peak_hist and now - self._peak_hist[0][0] > 3:
            self._peak_hist.pop(0)
        if now - self.meter.started < 1.5:
            return  # skip the start-up transient (it looks like clipping)
        r = self.silence.feed(top, now)
        name = self.config["audio_device"]
        if r == "silent":
            self.alerts.raise_("audio:silent", A.CRITICAL, "No audio",
                               f"{name}: silent for {self.silence.hold:.0f} s. "
                               "Muted channel, unplugged cable or console off?")
        elif r == "restored":
            self.alerts.clear("audio:silent", "Audio is back")
        if self.clip.feed(top, now):
            self.alerts.raise_("audio:clip", A.WARN, "Audio is clipping",
                               "Lower the gain on the console or interface.")
            once(20, lambda: self.alerts.clear("audio:clip"))
        if self.on_level_update:
            self.on_level_update()

    def _on_meter_error(self, message):
        self.meter_error = message
        self._changed()

    def recent_peak(self):
        return max((p for _, p in self._peak_hist), default=None)

    # ------------------------------------------------------------------ OBS
    def _on_obs_event(self, etype, data):
        if etype in ("StreamStateChanged", "RecordStateChanged"):
            self._changed()

    def _obs_alerts(self):
        obs = self.obs
        if not self.live:
            return
        if obs.state != "connected":
            self.alerts.raise_("obs:offline", A.CRITICAL, "Lost connection to OBS",
                               "OBS closed or crashed?")
            return
        self.alerts.clear("obs:offline", "OBS connection is back")
        for name, o in obs.outputs.items():
            label = output_label(o)
            was = self._prev_outputs.get(name)
            if was and not o.active and is_stream(o):
                self.alerts.raise_(f"out:{name}:stopped", A.CRITICAL, f"{label} stopped",
                                   "This output is no longer streaming.")
            if o.active:
                self.alerts.clear(f"out:{name}:stopped", f"{label} is streaming again")
            if o.reconnecting:
                self.alerts.raise_(f"out:{name}:reconn", A.CRITICAL, f"{label} is reconnecting",
                                   "The connection to the platform dropped.")
            else:
                self.alerts.clear(f"out:{name}:reconn")
            if not o.active:
                # A stopped output can't be dropping frames or congested.
                self.alerts.clear(f"out:{name}:drops")
                self.alerts.clear(f"out:{name}:cong")
            drop = o.drop_pct
            if o.active and drop is not None and drop >= 2:
                self.alerts.raise_(f"out:{name}:drops", A.WARN, f"{label}: dropping frames",
                                   f"{drop:.1f}% of frames dropped in the last 30 s. "
                                   "The upload can't keep up; lower the bitrate.")
            elif drop is not None and drop < 0.5:
                self.alerts.clear(f"out:{name}:drops")
            if o.active and o.congestion >= 0.6:
                self.alerts.raise_(f"out:{name}:cong", A.WARN, f"{label}: network congested")
            elif o.congestion < 0.3:
                self.alerts.clear(f"out:{name}:cong")
            if self.live and o.active and o.kbps:
                self.live.peak_out_kbps[label] = max(self.live.peak_out_kbps.get(label, 0), o.kbps)
                self.live.out_bytes[label] = o.bytes
        self._prev_outputs = {n: o.active for n, o in obs.outputs.items()}

        st, prev = obs.stats, self._prev_stats
        for key, akey, title, body in (
                ("renderSkippedFrames", "obs:render", "OBS is lagging (rendering)",
                 "The GPU can't keep up: reduce scenes/effects or the output resolution."),
                ("outputSkippedFrames", "obs:encode", "Encoder overloaded",
                 "Use NVENC, a faster preset, or a lower resolution/FPS.")):
            if key in st and key in prev and st[key] - prev[key] > 10:
                self.alerts.raise_(akey, A.WARN, title, body)
                once(30, lambda k=akey: self.alerts.clear(k))
        self._prev_stats = dict(st)

    # ---------------------------------------------------------------- ticks
    def _tick(self):
        self.net.sample()
        v = self.values
        v["cpu_load"] = self.cpu.sample()
        v["cpu_temp"] = cpu_temp()
        v["on_ac"], v["battery"] = power_state()
        self._pick_meter_device()
        self._net_alerts()
        self._system_alerts()
        self._obs_alerts()
        if self.live:
            self._record_sample()
        self._changed()
        return True

    def _slow(self):
        v = self.values
        v["sample_rate"] = sample_rate()
        v["profile"] = self.lpowerd.profile()
        path = recording_path()
        v["rec_path"] = path
        v["rec_free_gb"] = disk_free_gb(path) if path else None
        return True

    def _probe(self):
        for key in self.config["ingests"]:
            if key in INGESTS:
                _, host, port = INGESTS[key]
                self.prober.probe(key, host, port)
        return True

    def _net_alerts(self):
        n = self.net
        if not self.live:
            self._prev_net_kind = n.kind
            return
        if n.kind is None:
            self.alerts.raise_("net:down", A.CRITICAL, "Network is down",
                               "No internet route. Check the cable / Wi-Fi.")
        else:
            self.alerts.clear("net:down", "Network is back")
        if self._prev_net_kind and n.kind and n.kind != self._prev_net_kind:
            self.alerts.raise_("net:switch", A.WARN, f"Network switched to {n.kind}",
                               f"Was {self._prev_net_kind}.")
            once(60, lambda: self.alerts.clear("net:switch"))
        self._prev_net_kind = n.kind
        if n.kind == "wifi" and n.dbm is not None and n.dbm < -72:
            self.alerts.raise_("net:weak", A.WARN, "Weak Wi-Fi signal", f"{n.dbm:.0f} dBm")
        elif n.dbm is None or n.dbm > -67:
            self.alerts.clear("net:weak")
        for key in self.config["ingests"]:
            name = INGESTS.get(key, (key,))[0]
            if self.prober.results.get(key, 0) is None:
                self.alerts.raise_(f"ingest:{key}", A.WARN, f"{name} unreachable",
                                   "Can't reach the platform's ingest server.")
            else:
                self.alerts.clear(f"ingest:{key}")

    def _system_alerts(self):
        v = self.values
        if not self.live:
            return
        if v["on_ac"] is False:
            self.alerts.raise_("power:battery", A.CRITICAL, "Charger unplugged",
                               f"Running on battery ({v['battery']}%).")
        elif v["on_ac"]:
            self.alerts.clear("power:battery", "Charger connected again")
        ct, gt = v["cpu_temp"], self.gpu.temp
        if (ct or 0) >= 92 or (gt or 0) >= 87:
            self.alerts.raise_("sys:hot", A.WARN, "Laptop is very hot",
                               f"CPU {ct or 0:.0f} °C, GPU {gt or 0:.0f} °C. Check the vents.")
        elif (ct or 0) < 85 and (gt or 0) < 80:
            self.alerts.clear("sys:hot")
        free = v.get("rec_free_gb")
        if self.obs.recording and free is not None and free < 5:
            self.alerts.raise_("disk:low", A.WARN, "Recording disk almost full",
                               f"{free:.1f} GB left.")

    # ------------------------------------------------------------- snapshot
    def snapshot(self):
        v = self.values
        s = Snapshot()
        s.on_ac, s.battery = v.get("on_ac"), v.get("battery")
        s.profile = v.get("profile")
        s.net_kind, s.wifi_dbm, s.link_mbps = self.net.kind, self.net.dbm, self.net.link_mbps
        s.ingest_ms = dict(self.prober.results)
        s.obs_state = {"connecting": "offline"}.get(self.obs.state, self.obs.state)
        s.recording_free_gb = v.get("rec_free_gb")
        s.audio_device = self.config["audio_device"]
        s.audio_present = ("audio", s.audio_device) in self.devices.devices
        s.audio_peak_db = self.recent_peak() if s.audio_present else None
        s.sample_rate = v.get("sample_rate")
        s.video_expected = list(self.config["video_devices"])
        s.video_present = self.devices.names("video")
        s.cpu_temp, s.gpu_temp = v.get("cpu_temp"), self.gpu.temp
        return s

    def checks(self):
        names = {k: INGESTS[k][0] for k in self.config["ingests"] if k in INGESTS}
        return checklist(self.snapshot(), names)

    # ------------------------------------------------------------ live mode
    def go_live(self):
        if self.live:
            return
        self.live = LiveSession()
        self.alerts.muted = False
        self.inhibitor.acquire()
        if self.config["performance_when_live"]:
            prof = self.lpowerd.profile()
            if prof is not None and prof != "performance":
                self.live.prev_profile = prof
                self.lpowerd.set_profile("performance")
        self._prev_outputs = {n: o.active for n, o in self.obs.outputs.items()}
        self._prev_net_kind = self.net.kind
        # Alert right away about pinned devices that are already missing.
        for kind, name in (("audio", self.config["audio_device"]),
                           *(("video", n) for n in self.config["video_devices"])):
            if name and (kind, name) not in self.devices.devices:
                label = "Audio input" if kind == "audio" else "Capture device"
                self.alerts.raise_(f"device:{kind}:{name}", A.CRITICAL, f"{label} missing",
                                   f"{name} is not connected.")
        log.info("live session started")
        self._changed()

    def end_live(self):
        s = self.live
        if not s:
            return None
        self.live = None
        self.alerts.muted = True
        self.inhibitor.release()
        if s.prev_profile:
            self.lpowerd.set_profile(s.prev_profile)
        path = write_report(s, self.alerts.history, self.config)
        for key in list(self.alerts.active):
            self.alerts.dismiss(key)
        self.alerts.history = []
        log.info("live session ended, report: %s", path)
        self._changed()
        return path

    def _record_sample(self):
        s, v = self.live, self.values
        outs = {output_label(o): {"kbps": o.kbps, "drop": o.drop_pct}
                for o in self.obs.outputs.values() if o.active}
        s.samples.append({
            "t": round(s.elapsed, 1), "net": self.net.kind, "dbm": self.net.dbm,
            "tx_kbps": self.net.tx_bps and round(self.net.tx_bps / 1000),
            "cpu_temp": v.get("cpu_temp"), "gpu_temp": self.gpu.temp,
            "peak_db": self.recent_peak(), "outputs": outs})
        for k, val in (("cpu_temp", v.get("cpu_temp")), ("gpu_temp", self.gpu.temp)):
            if val is not None:
                s.max[k] = max(s.max[k] or val, val)
        if self.net.dbm is not None:
            s.min_dbm = min(s.min_dbm if s.min_dbm is not None else 0, self.net.dbm)


def output_label(o):
    pretty = {"simple_stream": "Main stream", "adv_stream": "Main stream",
              "simple_file_output": "Recording", "adv_file_output": "Recording",
              "adv_ffmpeg_output": "Recording"}
    return pretty.get(o.name, o.name)


def once(seconds, fn):
    def fire():
        fn()
        return False
    GLib.timeout_add_seconds(seconds, fire)


def reports_dir():
    docs = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
            or os.path.expanduser("~"))
    return os.path.join(docs, "StreamGuard")


def write_report(s, history, config):
    d = reports_dir()
    os.makedirs(d, exist_ok=True)
    stamp = datetime.fromtimestamp(s.started).strftime("%Y-%m-%d_%H%M")
    dur = int(s.elapsed)
    hms = f"{dur // 3600}:{dur % 3600 // 60:02d}:{dur % 60:02d}"
    lines = [f"# Stream report — {datetime.fromtimestamp(s.started):%d %b %Y, %H:%M}", "",
             f"- **Duration:** {hms}"]
    if s.max["cpu_temp"] is not None:
        lines.append(f"- **Max CPU temperature:** {s.max['cpu_temp']:.0f} °C")
    if s.max["gpu_temp"] is not None:
        lines.append(f"- **Max GPU temperature:** {s.max['gpu_temp']:.0f} °C")
    if s.min_dbm is not None:
        lines.append(f"- **Weakest Wi-Fi signal:** {s.min_dbm:.0f} dBm")
    if s.out_bytes:
        lines += ["", "## Outputs", "", "| Output | Data sent | Avg bitrate | Peak bitrate |",
                  "|---|---|---|---|"]
        for name, b in s.out_bytes.items():
            avg = b * 8 / 1000 / max(1, dur)
            lines.append(f"| {name} | {b / 1e9:.2f} GB | {avg:.0f} kb/s | "
                         f"{s.peak_out_kbps.get(name, 0):.0f} kb/s |")
    lines += ["", "## Alerts", ""]
    if not history:
        lines.append("No problems detected.")
    for a in history:
        t = datetime.fromtimestamp(a.wall).strftime("%H:%M:%S")
        dur_a = f" (lasted {int(a.cleared - a.wall)} s)" if a.cleared else ""
        lines.append(f"- `{t}` **{a.severity.upper()}** {a.title}{dur_a}"
                     + (f" — {a.body}" if a.body else ""))
    md = os.path.join(d, f"{stamp}.md")
    with open(md, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(d, f"{stamp}.json"), "w") as f:
        json.dump({"started": s.started, "duration_s": dur, "max": s.max, "min_dbm": s.min_dbm,
                   "outputs": s.out_bytes, "samples": s.samples,
                   "alerts": [vars(a) for a in history]}, f, indent=1)
    return md
