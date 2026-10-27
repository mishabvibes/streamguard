"""Pure detection logic (no I/O): silence, clipping, dropped frames, checklist.

Everything takes explicit timestamps so it can be unit-tested.
"""

from collections import deque
from dataclasses import dataclass, field

OK, WARN, FAIL, NA = "ok", "warn", "fail", "na"


class SilenceDetector:
    """Fires once when the peak level stays below a threshold for `hold` s."""

    def __init__(self, threshold_db=-50.0, hold=8.0):
        self.threshold = threshold_db
        self.hold = hold
        self.quiet_since = None
        self.fired = False
        self.last_sound = None

    def feed(self, peak_db, t):
        """Return 'silent', 'restored' or None."""
        if peak_db < self.threshold:
            if self.quiet_since is None:
                self.quiet_since = t
            if not self.fired and t - self.quiet_since >= self.hold:
                self.fired = True
                return "silent"
            return None
        self.last_sound = t
        self.quiet_since = None
        if self.fired:
            self.fired = False
            return "restored"
        return None

    def silent_for(self, t):
        return 0.0 if self.quiet_since is None else t - self.quiet_since


class ClipDetector:
    """Fires when the peak reaches `clip_db` `count` times inside `window` s."""

    def __init__(self, clip_db=-0.5, count=3, window=5.0, cooldown=30.0):
        self.clip_db = clip_db
        self.count = count
        self.window = window
        self.cooldown = cooldown
        self.hits = deque()
        self.last_fire = None

    def feed(self, peak_db, t):
        if peak_db >= self.clip_db:
            self.hits.append(t)
        while self.hits and t - self.hits[0] > self.window:
            self.hits.popleft()
        if len(self.hits) >= self.count and (self.last_fire is None
                                             or t - self.last_fire >= self.cooldown):
            self.last_fire = t
            self.hits.clear()
            return True
        return False


class RateTracker:
    """Turns monotonically increasing counters (bytes, frames) into rates."""

    def __init__(self, window=30.0):
        self.window = window
        self.samples = deque()

    def add(self, t, **counters):
        if self.samples:
            prev = self.samples[-1][1]
            if any(counters.get(k, 0) < v for k, v in prev.items()):
                self.samples.clear()  # counters reset (output restarted)
        self.samples.append((t, counters))
        while len(self.samples) > 2 and t - self.samples[0][0] > self.window:
            self.samples.popleft()

    def rate(self, key, span=None):
        """Per-second rate over the last `span` seconds (default: whole window)."""
        if len(self.samples) < 2:
            return None
        t1, c1 = self.samples[-1]
        t0, c0 = self.samples[0]
        if span is not None:
            for t, c in reversed(self.samples):
                t0, c0 = t, c
                if t1 - t >= span:
                    break
        dt = t1 - t0
        if dt <= 0 or key not in c1 or key not in c0:
            return None
        delta = c1[key] - c0[key]
        return None if delta < 0 else delta / dt  # counter reset -> unknown

    def ratio(self, num, den):
        """Delta(num)/Delta(den) over the window, e.g. dropped/total frames."""
        if len(self.samples) < 2:
            return None
        (_, c0), (_, c1) = self.samples[0], self.samples[-1]
        try:
            d = c1[den] - c0[den]
            n = c1[num] - c0[num]
        except KeyError:
            return None
        if d <= 0 or n < 0:
            return None
        return n / d


@dataclass
class Check:
    group: str
    title: str
    status: str
    detail: str = ""


@dataclass
class Snapshot:
    """Everything the checklist needs, gathered from the monitors."""
    on_ac: bool = None
    battery: int = None
    profile: str = None            # lpowerd profile, None if lpowerd absent
    net_kind: str = None           # ethernet | wifi | usb-tether | None
    wifi_dbm: float = None
    link_mbps: int = None
    ingest_ms: dict = field(default_factory=dict)   # key -> ms or None (unreachable)
    obs_state: str = None          # disabled | offline | connected
    recording_free_gb: float = None
    audio_device: str = None       # configured name
    audio_present: bool = None
    audio_peak_db: float = None    # recent peak
    sample_rate: int = None
    video_expected: list = field(default_factory=list)
    video_present: list = field(default_factory=list)
    cpu_temp: float = None
    gpu_temp: float = None
    links_healthy: int = None      # links with working internet
    failover: bool = False
    planned_mbps: float = None     # OBS main stream bitrate
    safe_mbps: float = None        # 70 % of best measured upload
    obs_reconnect: bool = None


def checklist(s, ingest_names):
    out = []
    add = lambda *a: out.append(Check(*a))

    # Power
    if s.on_ac is None:
        add("Power", "Charger", NA, "unknown")
    else:
        add("Power", "Charger", OK if s.on_ac else FAIL,
            "plugged in" if s.on_ac else f"on battery ({s.battery}%) — plug in before going live")
    if s.profile is None:
        add("Power", "Performance mode", NA, "lpowerd not installed")
    else:
        add("Power", "Performance mode", OK if s.profile == "performance" else WARN,
            s.profile if s.profile == "performance" else
            f"{s.profile} — GO LIVE switches to performance")

    # Network
    if s.net_kind is None:
        add("Network", "Connection", FAIL, "no network")
    elif s.net_kind == "ethernet":
        add("Network", "Connection", OK,
            f"Ethernet {s.link_mbps} Mb/s" if s.link_mbps else "Ethernet")
    elif s.net_kind == "wifi":
        if s.wifi_dbm is None:
            add("Network", "Connection", WARN, "Wi-Fi — use Ethernet if you can")
        else:
            st = OK if s.wifi_dbm >= -60 else WARN if s.wifi_dbm >= -70 else FAIL
            add("Network", "Connection", st,
                f"Wi-Fi {s.wifi_dbm:.0f} dBm ({signal_word(s.wifi_dbm)})"
                + ("" if st == OK else " — use Ethernet if you can"))
    else:
        add("Network", "Connection", WARN, s.net_kind)
    if s.failover and s.links_healthy is not None:
        add("Network", "Backup connection",
            OK if s.links_healthy >= 2 else WARN,
            f"{s.links_healthy} connections online" if s.links_healthy >= 2 else
            "only one connection — plug in Ethernet or phone USB tethering as a backup")
    if s.planned_mbps:
        if s.safe_mbps is None:
            add("Network", "Upload capacity", NA, f"run a speed test ({s.planned_mbps:.1f} Mb/s planned)")
        else:
            add("Network", "Upload capacity", OK if s.planned_mbps <= s.safe_mbps else WARN,
                f"{s.planned_mbps:.1f} of {s.safe_mbps:.1f} Mb/s safe" +
                ("" if s.planned_mbps <= s.safe_mbps else " — lower the bitrate"))
    for key, name in ingest_names.items():
        ms = s.ingest_ms.get(key, "pending")
        if ms == "pending":
            add("Network", name, NA, "checking…")
        elif ms is None:
            add("Network", name, FAIL, "ingest server unreachable")
        else:
            add("Network", name, OK if ms < 250 else WARN, f"{ms:.0f} ms")

    # OBS
    obs = {"connected": (OK, "connected"),
           "offline": (WARN, "OBS not running, or WebSocket server off"),
           "disabled": (WARN, "enable OBS → Tools → WebSocket Server Settings")}
    st, detail = obs.get(s.obs_state, (NA, "unknown"))
    add("OBS", "OBS link", st, detail)
    if s.obs_reconnect is False:
        add("OBS", "Auto-reconnect", WARN,
            "off — turn on in Settings → Advanced so OBS survives a network switch")
    if s.recording_free_gb is not None:
        gb = s.recording_free_gb
        add("OBS", "Recording space", OK if gb >= 20 else WARN if gb >= 5 else FAIL,
            f"{gb:.0f} GB free")

    # Audio
    if not s.audio_device:
        add("Audio", "Audio input", WARN, "choose your console/interface in Settings")
    elif not s.audio_present:
        add("Audio", "Audio input", FAIL, f"{s.audio_device} not connected")
    else:
        add("Audio", "Audio input", OK, s.audio_device)
        if s.audio_peak_db is None:
            add("Audio", "Signal", NA, "measuring…")
        elif s.audio_peak_db < -50:
            add("Audio", "Signal", WARN, "no signal — console on and unmuted?")
        elif s.audio_peak_db >= -0.5:
            add("Audio", "Signal", WARN, "clipping — lower the gain")
        else:
            add("Audio", "Signal", OK, f"peak {s.audio_peak_db:.0f} dB")
    if s.sample_rate:
        add("Audio", "Sample rate", OK if s.sample_rate == 48000 else WARN,
            f"{s.sample_rate} Hz" + ("" if s.sample_rate == 48000 else
                                     " — use 48000 Hz to avoid audio drift"))

    # Video
    if not s.video_expected:
        add("Video", "Capture devices", WARN, "choose your capture cards in Settings")
    for name in s.video_expected:
        ok = name in s.video_present
        add("Video", name, OK if ok else FAIL, "connected" if ok else "not connected")

    # System
    for label, temp in (("CPU temperature", s.cpu_temp), ("GPU temperature", s.gpu_temp)):
        if temp is not None:
            add("System", label, OK if temp < 80 else WARN if temp < 90 else FAIL, f"{temp:.0f} °C")
    return out


def signal_word(dbm):
    if dbm >= -55:
        return "excellent"
    if dbm >= -60:
        return "good"
    if dbm >= -70:
        return "fair"
    return "weak"


def overall(checks):
    states = {c.status for c in checks}
    if FAIL in states:
        return FAIL
    if WARN in states:
        return WARN
    return OK
