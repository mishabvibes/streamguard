"""Audio/video capture devices (GStreamer DeviceMonitor) and the audio level meter."""

import logging
import math
import re
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

log = logging.getLogger(__name__)
Gst.init(None)

_FPS = re.compile(r"(\d+)/(\d+)")


def channel_count(caps, limit=8):
    """Highest channel count a source offers (fixed value or range), capped."""
    best = 0
    for i in range(caps.get_size() if caps else 0):
        text = caps.get_structure(i).to_string()
        m = re.search(r"channels=\(int\)(?:\[\s*\d+,\s*(\d+)\s*\]|(\d+))", text)
        if m:
            best = max(best, int(m.group(1) or m.group(2)))
    return min(best, limit) or None


def video_modes(caps):
    """Best (width, height, fps) a capture device offers, e.g. (1920, 1080, 60)."""
    best = None
    if caps is None:
        return None
    for i in range(caps.get_size()):
        s = caps.get_structure(i)
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if not (ok_w and ok_h):
            continue
        text = s.to_string()
        fr = text.split("framerate=", 1)[1] if "framerate=" in text else ""
        fps = 0.0
        for n, d in _FPS.findall(fr.split(";")[0]):
            if int(d):
                fps = max(fps, int(n) / int(d))
        cand = (w, h, round(fps))
        if best is None or (w * h, fps) > (best[0] * best[1], best[2]):
            best = cand
    return best


class DeviceWatcher:
    """Tracks audio sources and video capture devices, with hotplug callbacks."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.devices = {}  # (kind, name) -> Gst.Device
        self.mon = Gst.DeviceMonitor.new()
        self.mon.add_filter("Audio/Source", None)
        self.mon.add_filter("Video/Source", None)
        self.mon.get_bus().add_watch(GLib.PRIORITY_DEFAULT, self._on_msg)
        self.running = False

    def start(self):
        if self.running:
            return
        self.mon.start()
        self.running = True
        self.devices = {}
        for d in self.mon.get_devices():
            self._add(d)
        self.on_change(None, None)

    def stop(self):
        if self.running:
            self.mon.stop()
            self.running = False

    @staticmethod
    def kind(dev):
        return "video" if dev.get_device_class().startswith("Video") else "audio"

    @staticmethod
    def is_monitor(dev):
        props = dev.get_properties()
        if props is None:
            return False
        cls = props.get_string("device.class") or ""
        return cls == "monitor" or (props.get_string("node.name") or "").endswith(".monitor")

    def _add(self, dev):
        if self.is_monitor(dev):
            return None
        key = (self.kind(dev), dev.get_display_name())
        self.devices[key] = dev
        return key

    def _on_msg(self, bus, msg):
        if msg.type == Gst.MessageType.DEVICE_ADDED:
            key = self._add(msg.parse_device_added())
            if key:
                self.on_change("added", key)
        elif msg.type == Gst.MessageType.DEVICE_REMOVED:
            dev = msg.parse_device_removed()
            key = (self.kind(dev), dev.get_display_name())
            if self.devices.pop(key, None) is not None:
                self.on_change("removed", key)
        return True

    def names(self, kind):
        return sorted(n for k, n in self.devices if k == kind)

    def describe(self, kind, name):
        dev = self.devices.get((kind, name))
        if dev is None:
            return None
        if kind == "video":
            m = video_modes(dev.get_caps())
            return f"{m[0]}×{m[1]} @ {m[2]} fps max" if m else ""
        caps = dev.get_caps()
        if caps and caps.get_size():
            ok, ch = caps.get_structure(0).get_int("channels")
            if ok:
                return f"{ch} ch"
        return ""


class LevelMeter:
    """Listens to one audio source and reports peak/RMS in dBFS, ~10 times a second.

    PipeWire lets several clients read the same source, so this works next to OBS.
    """

    def __init__(self, on_level, on_error):
        self.on_level = on_level
        self.on_error = on_error
        self.pipeline = None
        self.device_name = None
        self.started = 0.0

    def start(self, device):
        self.stop()
        self.device_name = device.get_display_name()
        src = device.create_element(None)
        if src is None:
            self.on_error(f"cannot open {self.device_name}")
            return
        p = Gst.Pipeline.new("meter")
        # Without caps PipeWire hands us a mono downmix; ask for every channel
        # so a console's left/right (or all interface inputs) are metered.
        caps = Gst.ElementFactory.make("capsfilter")
        ch = channel_count(device.get_caps())
        if ch:
            caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,channels={ch}"))
        conv = Gst.ElementFactory.make("audioconvert")
        level = Gst.ElementFactory.make("level")
        level.set_property("interval", 100 * Gst.MSECOND)
        level.set_property("post-messages", True)
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        for e in (src, caps, conv, level, sink):
            p.add(e)
        src.link(caps)
        caps.link(conv)
        conv.link(level)
        level.link(sink)
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message::element", self._on_element)
        bus.connect("message::error", self._on_bus_error)
        self.pipeline = p
        self.started = time.monotonic()
        p.set_state(Gst.State.PLAYING)

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline = None

    @staticmethod
    def _values(struct, name):
        try:
            v = struct.get_value(name)
            return [float(x) for x in v]
        except (TypeError, ValueError):
            text = struct.to_string()
            m = re.search(name + r"=\(GValueArray\)< ([^>]*) >", text)
            return [float(x.split(")")[-1]) for x in m.group(1).split(",")] if m else []

    def _on_element(self, bus, msg):
        s = msg.get_structure()
        if s is None or s.get_name() != "level":
            return
        peak = self._values(s, "peak")
        rms = self._values(s, "rms")
        clean = lambda xs: [x if math.isfinite(x) else -120.0 for x in xs]
        if peak:
            self.on_level(clean(peak), clean(rms))

    def _on_bus_error(self, bus, msg):
        err, _ = msg.parse_error()
        log.warning("meter error on %s: %s", self.device_name, err.message)
        self.stop()
        self.on_error(err.message)
