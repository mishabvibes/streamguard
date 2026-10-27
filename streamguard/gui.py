"""Stream Guard window: ready-check dashboard that becomes an on-air monitor."""

import json
import logging
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .config import INGESTS, Config  # noqa: E402
from .engine import Engine, fmt_bps, output_label  # noqa: E402
from .obs import is_stream  # noqa: E402
from .rules import FAIL, NA, OK, WARN  # noqa: E402

log = logging.getLogger(__name__)

CSS = """
window.sg { background-color: #0a0d12; color: #e6ebf2; }
window.sg headerbar { background: transparent; box-shadow: none; border: none; }
.brand { font-weight: 800; letter-spacing: 4px; font-size: 13px; color: #22d3ee; }
.brand-dim { color: alpha(#e6ebf2, 0.45); font-weight: 600; letter-spacing: 4px; font-size: 13px; }
window.sg.live .brand { color: #ff3b4e; }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 2.5px; color: alpha(#e6ebf2, 0.45); }
.card { background-image: linear-gradient(180deg, #141a24, #10151d);
  border: 1px solid #1e2632; border-radius: 16px; padding: 16px; box-shadow: none; }
.hero-title { font-size: 30px; font-weight: 800; font-feature-settings: "tnum"; }
.hero-sub { color: alpha(#e6ebf2, 0.6); }
.state-ok { color: #34d399; } .state-warn { color: #fbbf24; } .state-fail { color: #f87171; }
.state-na { color: alpha(#e6ebf2, 0.35); }
.onair { color: #ff3b4e; }
button.golive { background-color: #ff3b4e; color: white; font-weight: 800; font-size: 15px;
  letter-spacing: 1.5px; border-radius: 12px; padding: 14px 26px; box-shadow: none; }
button.golive:hover { background-color: #ff5566; }
button.endlive { background-color: #1c2430; color: #ff8f9b; font-weight: 800; font-size: 15px;
  letter-spacing: 1.5px; border-radius: 12px; padding: 14px 26px; border: 1px solid #ff3b4e; }
.alert-row { border-radius: 12px; padding: 10px 12px; }
.alert-critical { background-color: alpha(#ff3b4e, 0.14); border: 1px solid alpha(#ff3b4e, 0.55); }
.alert-warn { background-color: alpha(#fbbf24, 0.10); border: 1px solid alpha(#fbbf24, 0.45); }
.alert-title { font-weight: 700; }
.muted { color: alpha(#e6ebf2, 0.5); font-size: 12px; }
.big-value { font-size: 22px; font-weight: 800; font-feature-settings: "tnum"; }
.kv-key { color: alpha(#e6ebf2, 0.55); }
.kv-val { font-weight: 600; font-feature-settings: "tnum"; }
.dot { min-width: 8px; min-height: 8px; border-radius: 4px; }
.dot.ok { background-color: #34d399; } .dot.warn { background-color: #fbbf24; }
.dot.fail { background-color: #f87171; } .dot.na { background-color: #3a4456; }
.group-title { font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
  color: alpha(#e6ebf2, 0.45); margin-top: 6px; }
@keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.25; } 100% { opacity: 1; } }
.blink { animation: blink 1.6s ease-in-out infinite; }
"""

STATUS_CLASS = {OK: "ok", WARN: "warn", FAIL: "fail", NA: "na"}


def label(text="", *classes, xalign=0.0, **kw):
    # Cap natural width so wrapped/ellipsized text doesn't force tiles to full width.
    if kw.get("wrap") or kw.get("ellipsize"):
        kw.setdefault("max_width_chars", 34)
    return Gtk.Label(label=text, xalign=xalign, css_classes=list(classes), **kw)


def dot(status):
    return Gtk.Box(css_classes=["dot", STATUS_CLASS.get(status, "na")], valign=Gtk.Align.CENTER)


def clear(box):
    while (c := box.get_first_child()):
        box.remove(c)


def kv_row(key, value, status=None):
    row = Gtk.Box(spacing=8)
    if status:
        row.append(dot(status))
    row.append(label(key, "kv-key", hexpand=True, ellipsize=3, max_width_chars=18))
    row.append(label(value, "kv-val", xalign=1.0, ellipsize=3, max_width_chars=22))
    return row


class Meter(Gtk.Widget):
    """Per-channel peak meter, -60..0 dBFS, with green/amber/red zones and peak hold."""

    LO = -60.0

    def __init__(self):
        super().__init__()
        self.peak = []
        self.hold = []

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 160, 260, -1, -1
        n = max(2, len(self.peak))
        h = n * 10 + (n - 1) * 5
        return h, h, -1, -1

    def set_levels(self, peak):
        if len(self.hold) != len(peak):
            self.hold = list(peak)
        self.hold = [max(p, h - 0.6) for p, h in zip(peak, self.hold)]
        resize = len(peak) != len(self.peak)
        self.peak = list(peak)
        self.queue_resize() if resize else self.queue_draw()

    def _x(self, db, w):
        return max(0.0, min(1.0, (db - self.LO) / -self.LO)) * w

    @staticmethod
    def _rgba(h, a=1.0):
        c = Gdk.RGBA()
        c.parse(h)
        c.alpha = a
        return c

    def do_snapshot(self, snap):
        w = self.get_width()
        n = max(1, len(self.peak))
        bar = 10
        for i in range(n):
            y = i * (bar + 5)
            rect = Graphene.Rect().init(0, y, w, bar)
            rr = Gsk.RoundedRect()
            rr.init_from_rect(rect, 3)
            snap.push_rounded_clip(rr)
            snap.append_color(self._rgba("#ffffff", 0.06), rect)
            p = self.peak[i] if i < len(self.peak) else self.LO
            fill = self._x(p, w)
            for lo, hi, col in ((self.LO, -18, "#22c55e"), (-18, -6, "#f59e0b"), (-6, 0, "#ef4444")):
                x0, x1 = self._x(lo, w), min(self._x(hi, w), fill)
                if x1 > x0:
                    snap.append_color(self._rgba(col), Graphene.Rect().init(x0, y, x1 - x0, bar))
            if i < len(self.hold):
                hx = self._x(self.hold[i], w)
                snap.append_color(self._rgba("#ffffff", 0.85),
                                  Graphene.Rect().init(max(0, hx - 2), y, 2, bar))
            snap.pop()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Stream Guard", default_width=1080,
                         default_height=900, css_classes=["sg"])
        self.app = app
        self.engine = app.engine
        self._keys = {}
        self._clock = None
        self.set_size_request(380, 520)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        brand = Gtk.Box(spacing=6)
        brand.append(label("STREAM", "brand"))
        brand.append(label("GUARD", "brand-dim"))
        header.set_title_widget(brand)
        gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings",
                          css_classes=["flat"])
        gear.connect("clicked", lambda *_: self.open_settings())
        header.pack_end(gear)
        view.add_top_bar(header)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=900)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=16,
                       margin_end=16, margin_bottom=24, margin_top=4)
        clamp.set_child(body)
        scroller.set_child(clamp)
        view.set_content(scroller)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        body.append(self._build_hero())
        self.alert_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(self.alert_box)

        # Two masonry columns; a breakpoint stacks them on narrow windows.
        self.cols = Gtk.Box(spacing=14, homogeneous=True)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.cols.append(left)
        self.cols.append(right)
        body.append(self.cols)
        self.audio_card = self._tile(left, "AUDIO")
        self.obs_card = self._tile(left, "OBS OUTPUTS")
        self.net_card = self._tile(left, "NETWORK")
        self.check_card = self._tile(right, "READY CHECK")
        self.sys_card = self._tile(right, "SYSTEM")
        self.dev_card = self._tile(right, "DEVICES")
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        bp.add_setter(self.cols, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(bp)
        self._build_audio()
        for card in (self.obs_card, self.net_card, self.sys_card, self.dev_card, self.check_card):
            card.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.append(card.content)

        self.connect("close-request", self._on_close)
        self.engine.on_level_update = self._on_level

    # --------------------------------------------------------------- build
    def _tile(self, column, title):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card"])
        card.append(label(title, "eyebrow"))
        column.append(card)
        return card

    def _build_hero(self):
        card = Gtk.Box(spacing=16, css_classes=["card"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True,
                       valign=Gtk.Align.CENTER)
        self.hero_eyebrow = label("READY CHECK", "eyebrow")
        self.hero_title = label("Checking…", "hero-title", wrap=True)
        self.hero_sub = label("", "hero-sub", wrap=True)
        text.append(self.hero_eyebrow)
        text.append(self.hero_title)
        text.append(self.hero_sub)
        self.live_btn = Gtk.Button(label="GO LIVE", css_classes=["golive"],
                                   valign=Gtk.Align.CENTER)
        self.live_btn.connect("clicked", self._on_live_clicked)
        card.append(text)
        card.append(self.live_btn)
        return card

    def _build_audio(self):
        c = self.audio_card
        self.audio_name = label("", "kv-val", ellipsize=3)
        self.meter = Meter()
        scale = Gtk.Box()
        for t in ("-60", "-40", "-20", "-6", "0"):
            scale.append(label(t, "muted", hexpand=True, xalign={"-60": 0.0, "0": 1.0}.get(t, 0.5)))
        self.audio_state = label("", "muted", wrap=True)
        for w in (self.audio_name, self.meter, scale, self.audio_state):
            c.append(w)

    # -------------------------------------------------------------- update
    def _on_level(self):
        if self.get_mapped():
            self.meter.set_levels(self.engine.peak)

    def _changed(self, key, value):
        """True if `value` differs from last render for `key` (avoids rebuild flicker)."""
        if self._keys.get(key) == value:
            return False
        self._keys[key] = value
        return True

    def refresh(self):
        e = self.engine
        checks = e.checks()
        live = e.live is not None
        (self.add_css_class if live else self.remove_css_class)("live")
        self._refresh_hero(checks, live)
        self._refresh_alerts()
        self._refresh_audio()
        self._refresh_obs()
        self._refresh_net()
        self._refresh_sys()
        self._refresh_devices()
        self._refresh_checks(checks)

    def _refresh_hero(self, checks, live):
        if live:
            self.hero_eyebrow.set_label("● ON AIR")
            self.hero_eyebrow.set_css_classes(["eyebrow", "onair", "blink"])
            self._tick_clock()
            n = len(self.engine.alerts.active)
            self.hero_sub.set_label(f"{n} active alert{'s' if n != 1 else ''}" if n
                                    else "Everything looks healthy")
            self.live_btn.set_label("END STREAM")
            self.live_btn.set_css_classes(["endlive"])
            if self._clock is None:
                self._clock = GLib.timeout_add_seconds(1, self._tick_clock)
            return
        if self._clock:
            GLib.source_remove(self._clock)
            self._clock = None
        self.hero_eyebrow.set_label("READY CHECK")
        self.hero_eyebrow.set_css_classes(["eyebrow"])
        fails = [c for c in checks if c.status == FAIL]
        warns = [c for c in checks if c.status == WARN]
        if fails:
            self.hero_title.set_label(f"{len(fails)} problem{'s' if len(fails) > 1 else ''} to fix")
            self.hero_title.set_css_classes(["hero-title", "state-fail"])
            self.hero_sub.set_label(" · ".join(f"{c.title}: {c.detail}" for c in fails[:2]))
        elif warns:
            self.hero_title.set_label("Ready, with warnings")
            self.hero_title.set_css_classes(["hero-title", "state-warn"])
            self.hero_sub.set_label(" · ".join(c.title for c in warns[:3]))
        else:
            self.hero_title.set_label("Ready to go live")
            self.hero_title.set_css_classes(["hero-title", "state-ok"])
            self.hero_sub.set_label("All checks passed")
        self.live_btn.set_label("GO LIVE")
        self.live_btn.set_css_classes(["golive"])

    def _tick_clock(self):
        s = self.engine.live
        if not s:
            return False
        t = int(s.elapsed)
        self.hero_title.set_label(f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")
        self.hero_title.set_css_classes(["hero-title"])
        return True

    def _refresh_alerts(self):
        active = sorted(self.engine.alerts.active.values(),
                        key=lambda a: (a.severity != "critical", a.t))
        if not self._changed("alerts", [(a.key, a.title, a.body) for a in active]):
            return
        clear(self.alert_box)
        for a in active:
            row = Gtk.Box(spacing=10, css_classes=["alert-row", f"alert-{a.severity}"])
            row.append(Gtk.Image(icon_name="dialog-warning-symbolic"))
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            txt.append(label(a.title, "alert-title"))
            if a.body:
                txt.append(label(a.body, "muted", wrap=True))
            row.append(txt)
            btn = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"],
                             valign=Gtk.Align.CENTER, tooltip_text="Dismiss")
            btn.connect("clicked", lambda _b, k=a.key: self.engine.alerts.dismiss(k))
            row.append(btn)
            self.alert_box.append(row)

    def _refresh_audio(self):
        e = self.engine
        name = e.config["audio_device"]
        if not name:
            self.audio_name.set_label("No input selected — open Settings")
            self.audio_state.set_label("")
            self.meter.set_levels([])
            return
        present = ("audio", name) in e.devices.devices
        self.audio_name.set_label(name)
        if not present:
            self.audio_state.set_label("Not connected")
            self.meter.set_levels([])
        elif e.meter_error:
            self.audio_state.set_label(f"Meter error: {e.meter_error}")
        else:
            quiet = e.silence.silent_for(GLib.get_monotonic_time() / 1e6)
            peak = e.recent_peak()
            if quiet >= 3:
                self.audio_state.set_label(f"Silent for {quiet:.0f} s")
            elif peak is not None:
                self.audio_state.set_label(f"Signal OK · peak {peak:.0f} dBFS")

    def _refresh_obs(self):
        o, c = self.engine.obs, self.obs_card.content
        outs = [x for x in o.outputs.values() if x.active or is_stream(x)]
        key = (o.state, [(x.name, x.active, x.reconnecting, round(x.kbps or 0, -1),
                          round(x.drop_pct or 0, 1), round(x.congestion, 1)) for x in outs],
               round(o.stats.get("activeFps", 0)), round(o.stats.get("cpuUsage", 0)))
        if not self._changed("obs", key):
            return
        clear(c)
        if o.state != "connected":
            msg = {"disabled": "Turn on OBS → Tools → WebSocket Server Settings → "
                               "Enable WebSocket server. Stream Guard reads the password itself.",
                   "connecting": "Connecting…"}.get(o.state, o.error or "Waiting for OBS to start…")
            c.append(kv_row("OBS", "not connected", WARN))
            c.append(label(msg, "muted", wrap=True))
            return
        st = o.stats
        c.append(kv_row("Render", f"{st.get('activeFps', 0):.0f} fps · "
                                  f"CPU {st.get('cpuUsage', 0):.0f}%", OK))
        if not outs:
            c.append(label("No outputs yet. Start streaming or recording in OBS.", "muted",
                           wrap=True))
        for x in outs:
            status = (FAIL if x.reconnecting else WARN if (x.drop_pct or 0) >= 2 or
                      x.congestion >= 0.6 else OK if x.active else NA)
            val = "off" if not x.active else (
                "reconnecting…" if x.reconnecting else
                f"{(x.kbps or 0) / 1000:.1f} Mb/s · drop {x.drop_pct or 0:.1f}%")
            c.append(kv_row(output_label(x), val, status))

    def _refresh_net(self):
        e, c = self.engine, self.net_card.content
        n = e.net
        ingests = [(INGESTS[k][0], e.prober.results.get(k, "…")) for k in e.config["ingests"]
                   if k in INGESTS]
        key = (n.kind, n.dbm, n.link_mbps, round((n.tx_bps or 0) / 1e5), ingests)
        if not self._changed("net", key):
            return
        clear(c)
        if n.kind is None:
            c.append(kv_row("Connection", "offline", FAIL))
        else:
            kind = {"wifi": "Wi-Fi", "ethernet": "Ethernet", "usb-tether": "USB tether"}[n.kind]
            detail = (f"{n.dbm:.0f} dBm" if n.dbm is not None else
                      f"{n.link_mbps} Mb/s link" if n.link_mbps else n.iface)
            st = OK if n.kind == "ethernet" or (n.dbm or -100) >= -60 else WARN
            c.append(kv_row(kind, detail, st))
        big = Gtk.Box(spacing=8)
        big.append(label(fmt_bps(n.tx_bps), "big-value"))
        big.append(label("upload now", "muted", valign=Gtk.Align.END, margin_bottom=4))
        c.append(big)
        for name, ms in ingests:
            if ms == "…":
                c.append(kv_row(name, "checking…", NA))
            elif ms is None:
                c.append(kv_row(name, "unreachable", FAIL))
            else:
                c.append(kv_row(name, f"{ms:.0f} ms", OK if ms < 250 else WARN))

    def _refresh_sys(self):
        e, c = self.engine, self.sys_card.content
        v, g = e.values, e.gpu
        key = (v.get("on_ac"), v.get("battery"), round(v.get("cpu_temp") or 0),
               round(v.get("cpu_load") or 0, -1), g.temp, g.enc, round(g.watts or 0),
               v.get("profile"), round(v.get("rec_free_gb") or 0))
        if not self._changed("sys", key):
            return
        clear(c)
        on_ac = v.get("on_ac")
        c.append(kv_row("Power", "charger" if on_ac else f"battery {v.get('battery')}%",
                        OK if on_ac else FAIL))
        if v.get("profile"):
            c.append(kv_row("Profile", v["profile"], OK if v["profile"] == "performance" else WARN))
        ct = v.get("cpu_temp")
        if ct is not None:
            c.append(kv_row("CPU", f"{ct:.0f} °C · {v.get('cpu_load') or 0:.0f}%",
                            OK if ct < 85 else WARN))
        if g.temp is not None:
            c.append(kv_row("GPU", f"{g.temp:.0f} °C · encoder {g.enc or 0:.0f}% · "
                                   f"{g.watts or 0:.0f} W", OK if g.temp < 80 else WARN))
        free = v.get("rec_free_gb")
        if free is not None:
            c.append(kv_row("Recording disk", f"{free:.0f} GB free",
                            OK if free >= 20 else WARN if free >= 5 else FAIL))

    def _refresh_devices(self):
        e, c = self.engine, self.dev_card.content
        cfg = e.config
        video = sorted(set(e.devices.names("video")) | set(cfg["video_devices"]))
        audio = e.devices.names("audio")
        key = (video, audio, tuple(cfg["video_devices"]), cfg["audio_device"])
        if not self._changed("dev", key):
            return
        clear(c)
        c.append(label("CAPTURE", "group-title"))
        for name in video:
            present = ("video", name) in e.devices.devices
            pinned = name in cfg["video_devices"]
            st = (OK if present else FAIL) if pinned else NA
            c.append(kv_row(name, (e.devices.describe("video", name) or "connected")
                            if present else "not connected", st))
        if not video:
            c.append(label("No capture devices", "muted"))
        c.append(label("AUDIO INPUTS", "group-title"))
        for name in audio:
            c.append(kv_row(name, "watching" if name == cfg["audio_device"] else
                            e.devices.describe("audio", name) or "",
                            OK if name == cfg["audio_device"] else NA))

    def _refresh_checks(self, checks):
        c = self.check_card.content
        key = [(x.group, x.title, x.status, x.detail) for x in checks]
        if not self._changed("checks", key):
            return
        clear(c)
        group = None
        for x in checks:
            if x.group != group:
                group = x.group
                c.append(label(group.upper(), "group-title"))
            row = kv_row(x.title, "", x.status)
            row.set_tooltip_text(x.detail)
            row.get_last_child().set_label(x.detail)
            row.get_last_child().set_ellipsize(3)
            row.get_last_child().set_max_width_chars(28)
            c.append(row)

    # ------------------------------------------------------------- actions
    def _on_live_clicked(self, _btn):
        e = self.engine
        if e.live:
            dlg = Adw.AlertDialog(heading="End the stream session?",
                                  body="Stream Guard stops watching and saves a report. "
                                       "This does not stop OBS.")
            dlg.add_response("cancel", "Keep watching")
            dlg.add_response("end", "End session")
            dlg.set_response_appearance("end", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.connect("response", lambda d, r: r == "end" and self._end())
            dlg.present(self)
            return
        fails = [c for c in e.checks() if c.status == FAIL]
        if fails:
            dlg = Adw.AlertDialog(heading=f"{len(fails)} problem{'s' if len(fails) > 1 else ''} found",
                                  body="\n".join(f"• {c.title}: {c.detail}" for c in fails))
            dlg.add_response("cancel", "Fix first")
            dlg.add_response("go", "Go live anyway")
            dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.connect("response", lambda d, r: r == "go" and self._go())
            dlg.present(self)
        else:
            self._go()

    def _go(self):
        self.engine.go_live()
        self.toast("Watching your stream. Alerts are on.")

    def _end(self):
        path = self.engine.end_live()
        if path:
            t = Adw.Toast(title="Report saved", button_label="Open", timeout=8)
            t.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(path), None))
            self.toasts.add_toast(t)

    def _on_close(self, _win):
        if not self.engine.live:
            return False
        dlg = Adw.AlertDialog(heading="You are live",
                              body="Closing Stream Guard ends the session and turns alerts off.")
        dlg.add_response("cancel", "Keep open")
        dlg.add_response("quit", "End & close")
        dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(d, r):
            if r == "quit":
                self._end()
                self.engine.stop()
                self.app.quit()
        dlg.connect("response", resp)
        dlg.present(self)
        return True

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def open_settings(self):
        SettingsDialog(self).present(self)


def list_sinks():
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sinks"], capture_output=True,
                             text=True, timeout=3).stdout
        return [(s["name"], s.get("description") or s["name"]) for s in json.loads(out)]
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyError):
        return []


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Settings", search_enabled=False)
        self.win = win
        self.engine = win.engine
        self.cfg = win.engine.config
        self.connect("closed", self._save)

        # Devices
        page = Adw.PreferencesPage(title="Devices", icon_name="audio-input-microphone-symbolic")
        g = Adw.PreferencesGroup(title="Audio input to watch",
                                 description="Your mixer/console or audio interface. "
                                             "Stream Guard alarms if it goes silent or disconnects.")
        self.audio_names = [None] + self.engine.devices.names("audio")
        cur = self.cfg["audio_device"]
        if cur and cur not in self.audio_names:
            self.audio_names.append(cur)
        self.audio_row = Adw.ComboRow(title="Input")
        self.audio_row.set_model(Gtk.StringList.new(
            ["None"] + [n + ("" if ("audio", n) in self.engine.devices.devices else " (not connected)")
                        for n in self.audio_names[1:]]))
        self.audio_row.set_selected(self.audio_names.index(cur) if cur in self.audio_names else 0)
        g.add(self.audio_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Capture devices that must stay connected",
                                 description="Plug them in to see them here.")
        self.video_rows = {}
        names = sorted(set(self.engine.devices.names("video")) | set(self.cfg["video_devices"]))
        for n in names:
            r = Adw.SwitchRow(title=n, active=n in self.cfg["video_devices"])
            if ("video", n) in self.engine.devices.devices:
                r.set_subtitle(self.engine.devices.describe("video", n) or "")
            else:
                r.set_subtitle("not connected")
            self.video_rows[n] = r
            g.add(r)
        if not names:
            g.add(Adw.ActionRow(title="No capture devices found"))
        page.add(g)
        self.add(page)

        # Alerts
        page = Adw.PreferencesPage(title="Alerts", icon_name="dialog-warning-symbolic")
        g = Adw.PreferencesGroup(title="Silence detection")
        self.silence_db = Adw.SpinRow.new_with_range(-70, -30, 1)
        self.silence_db.set_title("Silent below (dBFS)")
        self.silence_db.set_value(self.cfg["silence_db"])
        self.silence_s = Adw.SpinRow.new_with_range(3, 60, 1)
        self.silence_s.set_title("Alert after (seconds)")
        self.silence_s.set_value(self.cfg["silence_seconds"])
        g.add(self.silence_db)
        g.add(self.silence_s)
        page.add(g)

        g = Adw.PreferencesGroup(
            title="Phone alerts",
            description="Install the free ntfy app on your phone and subscribe to a topic name "
                        "only you know (e.g. a long random word). Alerts while live go there.")
        self.ntfy = Adw.EntryRow(title="ntfy topic", text=self.cfg["ntfy_topic"] or "")
        g.add(self.ntfy)
        test = Adw.ActionRow(title="Send a test alert")
        btn = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._test)
        test.add_suffix(btn)
        g.add(test)
        page.add(g)

        g = Adw.PreferencesGroup(
            title="Alert sound",
            description="Pick an output OBS does NOT capture (e.g. your headphones), or the "
                        "alarm may be heard on the stream.")
        self.sound = Adw.SwitchRow(title="Play a sound", active=self.cfg["alert_sound"])
        self.sinks = [(None, "Default output")] + list_sinks()
        self.sink_row = Adw.ComboRow(title="Play on")
        self.sink_row.set_model(Gtk.StringList.new([d for _, d in self.sinks]))
        names_ = [n for n, _ in self.sinks]
        self.sink_row.set_selected(names_.index(self.cfg["alert_sink"])
                                   if self.cfg["alert_sink"] in names_ else 0)
        g.add(self.sound)
        g.add(self.sink_row)
        page.add(g)
        self.add(page)

        # Platforms & integration
        page = Adw.PreferencesPage(title="Platforms", icon_name="network-transmit-symbolic")
        g = Adw.PreferencesGroup(title="Check the connection to",
                                 description="Measures how fast each platform's ingest server answers.")
        self.ingest_rows = {}
        for key, (name, host, port) in INGESTS.items():
            r = Adw.SwitchRow(title=name, subtitle=f"{host}:{port}",
                              active=key in self.cfg["ingests"])
            self.ingest_rows[key] = r
            g.add(r)
        page.add(g)
        g = Adw.PreferencesGroup(title="While live")
        have_lp = self.engine.values.get("profile") is not None
        self.perf = Adw.SwitchRow(
            title="Switch laptop to Performance mode",
            subtitle="Uses lpowerd; restored when the session ends" if have_lp
            else "Needs lpowerd (github.com/mishabvibes/lpowerd)",
            active=self.cfg["performance_when_live"], sensitive=have_lp)
        g.add(self.perf)
        g.add(Adw.ActionRow(title="Sleep and screen lock are blocked while live"))
        page.add(g)
        g = Adw.PreferencesGroup(title="OBS",
                                 description="Normally read from OBS automatically. Only fill "
                                             "this in if OBS runs with a different password.")
        self.obs_pw = Adw.PasswordEntryRow(title="WebSocket password override",
                                           text=self.cfg["obs_password"] or "")
        g.add(self.obs_pw)
        page.add(g)
        self.add(page)

    def _collect(self):
        sel = self.audio_row.get_selected()
        return dict(
            audio_device=self.audio_names[sel] if 0 <= sel < len(self.audio_names) else None,
            video_devices=[n for n, r in self.video_rows.items() if r.get_active()],
            silence_db=self.silence_db.get_value(),
            silence_seconds=int(self.silence_s.get_value()),
            ntfy_topic=self.ntfy.get_text().strip(),
            alert_sound=self.sound.get_active(),
            alert_sink=self.sinks[self.sink_row.get_selected()][0],
            ingests=[k for k, r in self.ingest_rows.items() if r.get_active()],
            performance_when_live=self.perf.get_active(),
            obs_password=self.obs_pw.get_text() or None,
        )

    def _test(self, *_):
        self.cfg.set(**self._collect())
        self.engine.alerts.test()
        self.add_toast(Adw.Toast(title="Test alert sent"))

    def _save(self, *_):
        old_pw = self.cfg["obs_password"]
        self.cfg.set(**self._collect())
        if self.cfg["obs_password"] != old_pw:
            self.engine.obs.password_override = self.cfg["obs_password"]
            self.engine.obs.stop()
            self.engine.obs.start()
        self.engine.reconfigure()
        self.win._keys.clear()
        self.win.refresh()


class App(Adw.Application):
    def __init__(self, **kw):
        super().__init__(application_id=kw.pop("application_id", APP_ID), **kw)
        self.win = None
        self.engine = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.get_style_manager().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.engine = Engine(Config(), self._on_update)

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
            self.engine.start()
            if not self.engine.config["audio_device"]:
                GLib.timeout_add(600, lambda: self.win.open_settings() or False)
            shot = os.environ.get("STREAMGUARD_SHOT")
            if shot:
                GLib.timeout_add(int(os.environ.get("STREAMGUARD_SHOT_MS", "6000")),
                                 self._screenshot, shot)
        self.win.present()

    def do_shutdown(self):
        if self.engine:
            self.engine.stop()
        Adw.Application.do_shutdown(self)

    def _on_update(self):
        if self.win:
            self.win.refresh()

    def _screenshot(self, path):
        w = self.win
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(w).snapshot(snap, w.get_width(), w.get_height())
        node = snap.to_node()
        if node:
            w.get_native().get_renderer().render_texture(node, None).save_to_png(path)
        self.quit()
        return False


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    DBusGMainLoop(set_as_default=True)
    if "--version" in sys.argv:
        print(__version__)
        return 0
    return App().run(sys.argv[:1])
