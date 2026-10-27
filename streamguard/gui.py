"""Stream Guard window: a broadcast control-room dashboard.

Ready check before the show, on-air monitor during it. Every graph is drawn
with GSK on the GPU and only redraws when its data changes.
"""

import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .config import INGESTS, Config  # noqa: E402
from .engine import Engine, output_label  # noqa: E402
from .links import safe_bitrate_mbps  # noqa: E402
from .obs import is_stream  # noqa: E402
from .rules import FAIL, NA, OK, WARN  # noqa: E402
from .settings import SettingsDialog  # noqa: E402
from .widgets import LedMeter, Ring, Sparkline  # noqa: E402

log = logging.getLogger(__name__)

CYAN, GREEN, AMBER, RED, VIOLET = "#22d3ee", "#34d399", "#fbbf24", "#ff3b4e", "#a78bfa"
STATUS_COLOR = {OK: GREEN, WARN: AMBER, FAIL: RED, NA: "#3a4456"}
STATUS_CLASS = {OK: "ok", WARN: "warn", FAIL: "fail", NA: "na"}

CSS = """
window.sg { background-color: #07090d; color: #e8edf4; }
window.sg headerbar { background-color: transparent; box-shadow: none; border: none;
  min-height: 52px; }

/* brand + status pill */
.brand { font-weight: 800; letter-spacing: 4px; font-size: 13px; color: #22d3ee; }
.brand-dim { font-weight: 600; letter-spacing: 4px; font-size: 13px; color: alpha(#e8edf4, 0.4); }
window.sg.live .brand { color: #ff3b4e; }
.status-pill { border-radius: 999px; padding: 4px 12px 4px 10px; font-size: 11px;
  font-weight: 800; letter-spacing: 1.5px; background-color: alpha(#22d3ee, 0.12);
  color: #22d3ee; border: 1px solid alpha(#22d3ee, 0.35); }
.status-pill .pill-dot { min-width: 7px; min-height: 7px; border-radius: 4px;
  background-color: #22d3ee; }
window.sg.live .status-pill { background-color: alpha(#ff3b4e, 0.14); color: #ff6b7a;
  border-color: alpha(#ff3b4e, 0.5); }
window.sg.live .status-pill .pill-dot { background-color: #ff3b4e; }

/* surfaces: flat, thin border, no drop shadows */
.card { background-color: #0e131b; border: 1px solid #1a222e; border-radius: 18px;
  padding: 18px; box-shadow: none; }
.hero { background-image: linear-gradient(120deg, alpha(#22d3ee, 0.10), alpha(#22d3ee, 0.0) 55%),
  linear-gradient(#0e131b, #0e131b); }
window.sg.live .hero { background-image: linear-gradient(120deg, alpha(#ff3b4e, 0.14),
  alpha(#ff3b4e, 0.0) 55%), linear-gradient(#0e131b, #0e131b); }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 2.2px; color: alpha(#e8edf4, 0.45); }
.tile-icon { color: alpha(#e8edf4, 0.45); }

.hero-title { font-size: 34px; font-weight: 800; letter-spacing: -0.5px;
  font-feature-settings: "tnum"; }
.hero-sub { color: alpha(#e8edf4, 0.6); font-size: 14px; }
.ring-num { font-size: 26px; font-weight: 800; font-feature-settings: "tnum"; }
.ring-sub { font-size: 10px; font-weight: 700; letter-spacing: 1.5px; color: alpha(#e8edf4, 0.45); }
.c-ok { color: #34d399; } .c-warn { color: #fbbf24; } .c-fail { color: #ff6b7a; }
.c-live { color: #ff3b4e; }

button.golive { background-color: #ff3b4e; color: #fff; font-weight: 800; font-size: 15px;
  letter-spacing: 2px; border-radius: 14px; padding: 16px 30px; box-shadow: none;
  transition: background-color 200ms ease; }
button.golive:hover { background-color: #ff5265; }
button.endlive { background-color: transparent; color: #ff6b7a; font-weight: 800;
  font-size: 15px; letter-spacing: 2px; border-radius: 14px; padding: 16px 30px;
  border: 1.5px solid #ff3b4e; box-shadow: none; }
button.endlive:hover { background-color: alpha(#ff3b4e, 0.12); }

/* KPI tiles */
.kpi { padding: 14px 16px 10px 16px; }
.kpi-value { font-size: 28px; font-weight: 800; font-feature-settings: "tnum"; letter-spacing: -0.5px; }
.kpi-unit { font-size: 13px; font-weight: 700; color: alpha(#e8edf4, 0.5); }
.kpi-foot { font-size: 12px; color: alpha(#e8edf4, 0.5); }

/* rows */
.row-name { font-weight: 600; }
.row-sub { font-size: 12px; color: alpha(#e8edf4, 0.5); }
.num { font-weight: 700; font-feature-settings: "tnum"; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 11px; font-weight: 800;
  letter-spacing: 0.8px; }
.pill.ok { background-color: alpha(#34d399, 0.14); color: #34d399; }
.pill.warn { background-color: alpha(#fbbf24, 0.14); color: #fbbf24; }
.pill.fail { background-color: alpha(#ff3b4e, 0.16); color: #ff6b7a; }
.pill.na { background-color: alpha(#e8edf4, 0.07); color: alpha(#e8edf4, 0.5); }
.pill.live { background-color: #ff3b4e; color: #fff; }
.dot { min-width: 8px; min-height: 8px; border-radius: 4px; }
.dot.ok { background-color: #34d399; } .dot.warn { background-color: #fbbf24; }
.dot.fail { background-color: #ff3b4e; } .dot.na { background-color: #2c3544; }
.divider { background-color: #1a222e; min-height: 1px; }
.group-title { font-size: 10px; font-weight: 800; letter-spacing: 1.8px;
  color: alpha(#e8edf4, 0.4); margin-top: 4px; }
.check-detail { font-size: 12px; color: alpha(#e8edf4, 0.55); }
.muted { color: alpha(#e8edf4, 0.5); font-size: 12px; }
.meter-scale { font-size: 10px; color: alpha(#e8edf4, 0.4); font-feature-settings: "tnum"; }

progressbar.lat trough { min-height: 6px; border-radius: 3px; background-color: alpha(#ffffff, 0.06); }
progressbar.lat progress { min-height: 6px; border-radius: 3px; background-color: #22d3ee; }
progressbar.lat.warn progress { background-color: #fbbf24; }
progressbar.lat.fail progress { background-color: #ff3b4e; }

button.pill-btn { border-radius: 999px; padding: 4px 14px; font-size: 12px; font-weight: 800;
  box-shadow: none; }
button.pill-btn.go { background-color: alpha(#34d399, 0.16); color: #34d399; }
button.pill-btn.danger { background-color: alpha(#ff3b4e, 0.16); color: #ff6b7a; }
button.pill-btn.neutral { background-color: alpha(#e8edf4, 0.08); color: #e8edf4; }

/* alerts */
.alert { border-radius: 14px; padding: 12px 14px; }
.alert.critical { background-color: alpha(#ff3b4e, 0.12); border: 1px solid alpha(#ff3b4e, 0.55); }
.alert.warn { background-color: alpha(#fbbf24, 0.08); border: 1px solid alpha(#fbbf24, 0.4); }
.alert.critical image { color: #ff6b7a; } .alert.warn image { color: #fbbf24; }
.alert-title { font-weight: 800; }

@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.2; } 100% { opacity: 1; } }
window.sg.live .pill-dot { animation: pulse 1.4s ease-in-out infinite; }
@keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; } }
.rise { animation: rise 420ms cubic-bezier(.2,.8,.2,1) both; }
.d1 { animation-delay: 40ms; } .d2 { animation-delay: 90ms; } .d3 { animation-delay: 140ms; }
.d4 { animation-delay: 190ms; }
"""


def label(text="", *classes, xalign=0.0, **kw):
    if kw.get("wrap") or kw.get("ellipsize"):
        kw.setdefault("max_width_chars", 34)
    return Gtk.Label(label=text, xalign=xalign, css_classes=list(classes), **kw)


def clear(box):
    while (c := box.get_first_child()):
        box.remove(c)


def pill(text, status):
    return label(text, "pill", STATUS_CLASS.get(status, status), valign=Gtk.Align.CENTER)


def dot(status):
    return Gtk.Box(css_classes=["dot", STATUS_CLASS.get(status, "na")], valign=Gtk.Align.CENTER)


def short(name, n=26):
    return name if len(name) <= n else name[: n - 1] + "…"


class Kpi(Gtk.Box):
    """Big number + unit + sparkline + footnote."""

    def __init__(self, title, icon, unit, color, **spark):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         css_classes=["card", "kpi"], hexpand=True)
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Image(icon_name=icon, css_classes=["tile-icon"], pixel_size=14))
        head.append(label(title, "eyebrow"))
        self.append(head)
        val = Gtk.Box(spacing=4)
        self.value = label("—", "kpi-value")
        val.append(self.value)
        val.append(label(unit, "kpi-unit", valign=Gtk.Align.END, margin_bottom=5))
        self.append(val)
        self.spark = Sparkline(color, **spark)
        self.append(self.spark)
        self.foot = label("", "kpi-foot", ellipsize=3, max_width_chars=22)
        self.append(self.foot)

    def update(self, value, foot, values, color=None):
        self.value.set_label(value)
        self.foot.set_label(foot)
        self.spark.set_values(values, color)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Stream Guard", default_width=1200,
                         default_height=940, css_classes=["sg"])
        self.app = app
        self.engine = app.engine
        self._keys = {}
        self._clock = None
        self.set_size_request(380, 560)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        brand = Gtk.Box(spacing=6)
        brand.append(label("STREAM", "brand"))
        brand.append(label("GUARD", "brand-dim"))
        brand.set_margin_start(8)
        header.pack_start(brand)
        status = Gtk.Box(spacing=7, css_classes=["status-pill"], valign=Gtk.Align.CENTER)
        status.append(Gtk.Box(css_classes=["pill-dot"], valign=Gtk.Align.CENTER))
        self.status_lbl = label("READY CHECK")
        status.append(self.status_lbl)
        header.set_title_widget(status)
        gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings",
                          css_classes=["flat"])
        gear.connect("clicked", lambda *_: self.open_settings())
        header.pack_end(gear)
        view.add_top_bar(header)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1320, tightening_threshold=1000)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=20,
                       margin_end=20, margin_bottom=28, margin_top=4)
        clamp.set_child(body)
        scroller.set_child(clamp)
        view.set_content(scroller)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        body.append(self._build_hero())
        self.alert_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(self.alert_box)
        body.append(self._build_kpis())

        self.cols = Gtk.Box(spacing=16, css_classes=["rise", "d3"])
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, hexpand=True,
                       valign=Gtk.Align.START)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, width_request=380,
                        valign=Gtk.Align.START)
        self.cols.append(left)
        self.cols.append(right)
        body.append(self.cols)
        self.obs_card = self._card(left, "OBS OUTPUTS", "media-record-symbolic")
        self.net_card = self._card(left, "NETWORK", "network-wireless-symbolic")
        self.audio_card = self._card(right, "AUDIO", "audio-input-microphone-symbolic")
        self.dev_card = self._card(right, "DEVICES", "camera-video-symbolic")
        self.check_card = self._card(body, "READY CHECK", "emblem-ok-symbolic")
        self.check_card.add_css_class("rise")
        self.check_card.add_css_class("d4")
        self.check_cols = Gtk.Box(spacing=32, homogeneous=True)
        self.check_card.content.append(self.check_cols)
        self._build_audio()

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 940sp"))
        bp.add_setter(self.cols, "orientation", Gtk.Orientation.VERTICAL)
        bp.add_setter(right, "width-request", -1)
        bp.add_setter(self.kpi_flow, "min-children-per-line", 2)
        bp.add_setter(self.check_cols, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(bp)
        phone = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        phone.add_setter(self.cols, "orientation", Gtk.Orientation.VERTICAL)
        phone.add_setter(right, "width-request", -1)
        phone.add_setter(self.kpi_flow, "min-children-per-line", 2)
        phone.add_setter(self.check_cols, "orientation", Gtk.Orientation.VERTICAL)
        phone.add_setter(self.hero, "orientation", Gtk.Orientation.VERTICAL)
        phone.add_setter(self.ring_box, "halign", Gtk.Align.START)
        self.add_breakpoint(phone)

        self.connect("close-request", self._on_close)
        self.engine.on_level_update = self._on_level

    # --------------------------------------------------------------- build
    def _card(self, column, title, icon):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["card"])
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Image(icon_name=icon, css_classes=["tile-icon"], pixel_size=14))
        head.append(label(title, "eyebrow", hexpand=True))
        card.badge = Gtk.Box()
        head.append(card.badge)
        card.append(head)
        card.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.append(card.content)
        column.append(card)
        return card

    def _build_hero(self):
        card = Gtk.Box(spacing=24, css_classes=["card", "hero", "rise", "d1"])
        self.hero = card
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True,
                       valign=Gtk.Align.CENTER)
        self.hero_eyebrow = label("PRE-SHOW", "eyebrow")
        self.hero_title = label("Checking your setup…", "hero-title", wrap=True)
        self.hero_sub = label("", "hero-sub", wrap=True, max_width_chars=70)
        for w in (self.hero_eyebrow, self.hero_title, self.hero_sub):
            text.append(w)
        card.append(text)

        ring_box = Gtk.Overlay(valign=Gtk.Align.CENTER)
        self.ring_box = ring_box
        self.ring = Ring()
        ring_box.set_child(self.ring)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)
        self.ring_num = label("–", "ring-num", xalign=0.5)
        self.ring_sub = label("CHECKS", "ring-sub", xalign=0.5)
        center.append(self.ring_num)
        center.append(self.ring_sub)
        ring_box.add_overlay(center)
        card.append(ring_box)

        self.live_btn = Gtk.Button(label="GO LIVE", css_classes=["golive"],
                                   valign=Gtk.Align.CENTER)
        self.live_btn.connect("clicked", self._on_live_clicked)
        card.append(self.live_btn)
        return card

    def _build_kpis(self):
        self.kpi_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                    min_children_per_line=5, max_children_per_line=5,
                                    column_spacing=16, row_spacing=16,
                                    css_classes=["rise", "d2"])
        self.k_up = Kpi("UPLOAD", "network-transmit-symbolic", "Mb/s", CYAN, lo=0, min_span=1)
        self.k_drop = Kpi("DROPPED", "dialog-warning-symbolic", "%", GREEN, lo=0, min_span=2)
        self.k_audio = Kpi("AUDIO PEAK", "audio-volume-high-symbolic", "dBFS", VIOLET,
                           lo=-60, hi=0)
        self.k_cpu = Kpi("CPU", "computer-symbolic", "°C", AMBER, lo=30, hi=100)
        self.k_gpu = Kpi("GPU", "video-display-symbolic", "°C", AMBER, lo=30, hi=100)
        for k in (self.k_up, self.k_drop, self.k_audio, self.k_cpu, self.k_gpu):
            self.kpi_flow.append(k)
        return self.kpi_flow

    def _build_audio(self):
        c = self.audio_card.content
        self.audio_name = label("", "row-name", ellipsize=3)
        row = Gtk.Box(spacing=12)
        self.leds = LedMeter()
        self.leds.set_valign(Gtk.Align.START)
        # dB scale positioned to match the meter's linear -60..0 mapping.
        scale = Gtk.Fixed(width_request=26, height_request=LedMeter.HEIGHT,
                          valign=Gtk.Align.START)
        for db in (0, -6, -18, -30, -45, -60):
            scale.put(label(str(db), "meter-scale"), 0,
                      (LedMeter.HEIGHT - 12) * (-db / 60))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True,
                       valign=Gtk.Align.END)
        self.audio_state = label("", "row-sub", wrap=True, max_width_chars=22)
        self.audio_pill = Gtk.Box()
        info.append(self.audio_pill)
        info.append(self.audio_state)
        row.append(self.leds)
        row.append(scale)
        row.append(info)
        c.append(self.audio_name)
        c.append(row)

    # -------------------------------------------------------------- update
    def _on_level(self):
        if self.get_mapped():
            self.leds.set_levels(self.engine.peak)

    def _changed(self, key, value):
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
        self._refresh_kpis()
        self._refresh_audio()
        self._refresh_obs()
        self._refresh_net()
        self._refresh_devices()
        self._refresh_checks(checks)

    def _refresh_hero(self, checks, live):
        rated = [c for c in checks if c.status != NA]
        passed = sum(c.status == OK for c in rated)
        fails = [c for c in checks if c.status == FAIL]
        warns = [c for c in checks if c.status == WARN]
        color = RED if fails else AMBER if warns else GREEN
        self.ring.set_state(passed / len(rated) if rated else 0, color)
        self.ring_num.set_label(f"{passed}/{len(rated)}")

        if live:
            self.status_lbl.set_label("ON AIR")
            self.hero_eyebrow.set_label("LIVE SESSION")
            self.hero_eyebrow.set_css_classes(["eyebrow", "c-live"])
            n = len(self.engine.alerts.active)
            self.hero_sub.set_label(f"{n} active alert{'s' if n != 1 else ''} — see below" if n
                                    else "All systems healthy. Alerts go to your desktop and phone.")
            self.live_btn.set_label("END STREAM")
            self.live_btn.set_css_classes(["endlive"])
            self._tick_clock()
            if self._clock is None:
                self._clock = GLib.timeout_add_seconds(1, self._tick_clock)
            return
        if self._clock:
            GLib.source_remove(self._clock)
            self._clock = None
        self.status_lbl.set_label("READY CHECK")
        self.hero_eyebrow.set_label("PRE-SHOW")
        self.hero_eyebrow.set_css_classes(["eyebrow"])
        if fails:
            self.hero_title.set_label(f"{len(fails)} thing{'s' if len(fails) > 1 else ''} to fix")
            self.hero_title.set_css_classes(["hero-title", "c-fail"])
            self.hero_sub.set_label(" · ".join(f"{c.title}: {c.detail}" for c in fails[:2]))
        elif warns:
            self.hero_title.set_label("Ready, with warnings")
            self.hero_title.set_css_classes(["hero-title", "c-warn"])
            self.hero_sub.set_label(" · ".join(c.title for c in warns[:4]))
        else:
            self.hero_title.set_label("Ready to go live")
            self.hero_title.set_css_classes(["hero-title", "c-ok"])
            self.hero_sub.set_label("Every check passed. Break a leg.")
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
            row = Gtk.Box(spacing=12, css_classes=["alert", a.severity])
            row.append(Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=20,
                                 valign=Gtk.Align.START))
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            txt.append(label(a.title, "alert-title"))
            if a.body:
                txt.append(label(a.body, "row-sub", wrap=True, max_width_chars=90))
            row.append(txt)
            btn = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                             valign=Gtk.Align.CENTER, tooltip_text="Dismiss")
            btn.connect("clicked", lambda _b, k=a.key: self.engine.alerts.dismiss(k))
            row.append(btn)
            self.alert_box.append(row)

    def _refresh_kpis(self):
        e, h, n = self.engine, self.engine.history, self.engine.net
        up = (n.tx_bps or 0) / 1e6
        link = ({"wifi": f"Wi-Fi {n.dbm:.0f} dBm" if n.dbm is not None else "Wi-Fi",
                 "ethernet": f"Ethernet {n.link_mbps or ''} Mb/s".replace("  ", " "),
                 "usb-tether": "USB tether"}.get(n.kind, "offline"))
        self.k_up.update(f"{up:.1f}" if up >= 0.1 else f"{up:.2f}", link, h["tx"])

        drop = e.current_drop()
        streaming = any(o.active and is_stream(o) for o in e.obs.outputs.values())
        dcol = RED if (drop or 0) >= 2 else AMBER if (drop or 0) >= 0.5 else GREEN
        self.k_drop.update(f"{drop:.1f}" if streaming and drop is not None else "—",
                           "worst output, last 30 s" if streaming else "not streaming",
                           h["drop"], dcol)

        peak = e.recent_peak()
        quiet = e.silence.silent_for(GLib.get_monotonic_time() / 1e6)
        self.k_audio.update(f"{peak:.0f}" if peak is not None and peak > -100 else "—",
                            f"silent {quiet:.0f} s" if quiet >= 3 else
                            short(e.config["audio_device"] or "no input selected", 24), h["peak"],
                            RED if quiet >= 3 else VIOLET)

        v, g = e.values, e.gpu
        ct = v.get("cpu_temp")
        self.k_cpu.update(f"{ct:.0f}" if ct is not None else "—",
                          f"load {v.get('cpu_load') or 0:.0f}%", h["cpu"],
                          RED if (ct or 0) >= 90 else AMBER)
        self.k_gpu.update(f"{g.temp:.0f}" if g.temp is not None else "—",
                          f"encoder {g.enc or 0:.0f}% · {g.watts or 0:.0f} W"
                          if g.temp is not None else "no NVIDIA GPU", h["gpu"],
                          RED if (g.temp or 0) >= 85 else AMBER)

    def _refresh_audio(self):
        e = self.engine
        name = e.config["audio_device"]
        present = bool(name) and ("audio", name) in e.devices.devices
        quiet = e.silence.silent_for(GLib.get_monotonic_time() / 1e6)
        peak = e.recent_peak()
        if not name:
            st, txt, sub = NA, "NOT SET", "Pick your console in Settings"
        elif not present:
            st, txt, sub = FAIL, "DISCONNECTED", "Check the USB cable"
        elif e.meter_error:
            st, txt, sub = FAIL, "ERROR", e.meter_error
        elif quiet >= 3:
            st, txt, sub = FAIL if quiet >= e.silence.hold else WARN, "SILENT", f"for {quiet:.0f} s"
        elif peak is not None and peak >= -0.5:
            st, txt, sub = WARN, "CLIPPING", "lower the gain"
        else:
            st, txt, sub = OK, "SIGNAL OK", f"peak {peak:.0f} dBFS" if peak is not None else ""
        self.audio_name.set_label(short(name, 40) if name else "No input selected")
        if self._changed("audio-pill", (st, txt)):
            clear(self.audio_pill)
            self.audio_pill.append(pill(txt, st))
        self.audio_state.set_label(sub)
        if not present:
            self.leds.set_levels([])

    def _refresh_obs(self):
        e, card = self.engine, self.obs_card
        o = e.obs
        outs = [x for x in o.outputs.values() if x.active or is_stream(x)]
        st = o.stats
        key = (o.state, o.streaming, o.recording, [(x.name, x.active, x.reconnecting, round(x.kbps or 0, -1),
                          round(x.drop_pct or 0, 1)) for x in outs],
               round(st.get("activeFps", 0)), round(st.get("cpuUsage", 0)),
               [tuple(e.out_history.get(x.name, ())) for x in outs])
        if not self._changed("obs", key):
            return
        clear(card.badge)
        clear(card.content)
        c = card.content
        if o.state != "connected":
            card.badge.append(pill("OFFLINE", WARN))
            c.append(label("OBS isn't connected", "row-name"))
            c.append(label({"disabled": "In OBS: Tools → WebSocket Server Settings → Enable "
                                        "WebSocket server. Stream Guard reads the password itself."
                            }.get(o.state, o.error or "Waiting for OBS to start…"),
                           "row-sub", wrap=True, max_width_chars=70))
            return
        card.badge.append(pill(f"{st.get('activeFps', 0):.0f} FPS · CPU {st.get('cpuUsage', 0):.0f}%",
                               OK))
        ctl = Gtk.Box(spacing=8)
        sbtn = Gtk.Button(label="Stop stream" if o.streaming else "Start stream",
                          css_classes=["pill-btn", "danger" if o.streaming else "go"])
        sbtn.connect("clicked", lambda *_: self._obs_toggle("stream"))
        rbtn = Gtk.Button(label="Stop recording" if o.recording else "Record",
                          css_classes=["pill-btn", "danger" if o.recording else "neutral"])
        rbtn.connect("clicked", lambda *_: self._obs_toggle("record"))
        ctl.append(sbtn)
        ctl.append(rbtn)
        c.append(ctl)
        if not outs:
            c.append(label("No active outputs. Start streaming or recording in OBS.", "row-sub",
                           wrap=True))
            return
        for i, x in enumerate(outs):
            if i:
                c.append(Gtk.Box(css_classes=["divider"]))
            row = Gtk.Box(spacing=14)
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                           valign=Gtk.Align.CENTER)
            top = Gtk.Box(spacing=8)
            top.append(label(output_label(x), "row-name", ellipsize=3, max_width_chars=24))
            if x.reconnecting:
                top.append(pill("RECONNECTING", FAIL))
            elif x.active:
                top.append(pill("LIVE" if is_stream(x) else "REC", "live"))
            else:
                top.append(pill("OFF", NA))
            left.append(top)
            drop = x.drop_pct or 0
            sub = (f"{(x.kbps or 0) / 1000:.1f} Mb/s · dropped {drop:.1f}%"
                   if x.active else "not running")
            left.append(label(sub, "row-sub"))
            row.append(left)
            sp = Sparkline(RED if drop >= 2 else CYAN, height=34, lo=0, min_span=1)
            sp.set_size_request(200, -1)
            sp.set_values(e.out_history.get(x.name, ()))
            row.append(sp)
            c.append(row)

    def _refresh_net(self):
        e, card = self.engine, self.net_card
        links = sorted(e.links.links.values(), key=lambda l: l.metric)
        ingests = [(k, INGESTS[k][0], e.prober.results.get(k, "…")) for k in e.config["ingests"]
                   if k in INGESTS]
        planned = e.values.get("planned_mbps")
        key = ([(l.dev, l.metric, l.healthy, round(l.rtt_ms or 0, -1), l.up_mbps, l.down_mbps,
                 l.testing, tuple(l.platforms)) for l in links], ingests, planned,
               dict(e.platform_dev), bool(e.links.promoted), e.net.dbm)
        if not self._changed("net", key):
            return
        clear(card.badge)
        clear(card.content)
        c = card.content
        online = [l for l in links if l.healthy]
        if not links:
            card.badge.append(pill("OFFLINE", FAIL))
        elif e.links.promoted:
            card.badge.append(pill("ON BACKUP", WARN))
        else:
            card.badge.append(pill(f"{len(online)} LINK{'S' if len(online) != 1 else ''} ONLINE",
                                   OK if len(online) >= 2 else WARN))

        for i, l in enumerate(links):
            if i:
                c.append(Gtk.Box(css_classes=["divider"]))
            row = Gtk.Box(spacing=12)
            icon = {"ethernet": "network-wired-symbolic", "wifi": "network-wireless-symbolic",
                    "usb-tether": "phone-symbolic"}.get(l.kind, "network-workgroup-symbolic")
            row.append(Gtk.Image(icon_name=icon, pixel_size=20, css_classes=["tile-icon"],
                                 valign=Gtk.Align.CENTER))
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            top = Gtk.Box(spacing=8)
            top.append(label(l.label, "row-name"))
            top.append(pill("PRIMARY" if i == 0 else "BACKUP", "live" if i == 0 else NA))
            if l.healthy is False:
                top.append(pill("NO INTERNET", FAIL))
            for k in l.platforms:
                ok = e.platform_dev.get(k) in (None, l.dev)
                top.append(pill(INGESTS[k][0].upper() + ("" if ok else " ✗"), OK if ok else WARN))
            txt.append(top)
            parts = [l.dev]
            if l.kind == "wifi" and e.net.iface == l.dev and e.net.dbm is not None:
                parts.append(f"{e.net.dbm:.0f} dBm")
            if l.rtt_ms is not None:
                parts.append(f"{l.rtt_ms:.0f} ms")
            if l.up_mbps is not None:
                parts.append(f"↑ {l.up_mbps:.1f} ↓ {l.down_mbps or 0:.0f} Mb/s")
            txt.append(label(" · ".join(parts), "row-sub"))
            row.append(txt)
            btn = Gtk.Button(label="Testing…" if l.testing else "Speed test",
                             css_classes=["pill-btn", "neutral"], valign=Gtk.Align.CENTER,
                             sensitive=not l.testing and l.healthy is not False,
                             tooltip_text="Uploads up to 30 MB and downloads for 7 s "
                                          "through this connection only")
            btn.connect("clicked", lambda _b, d=l.dev: self._speed_test(d))
            row.append(btn)
            c.append(row)

        safe = safe_bitrate_mbps(links)
        if planned or safe:
            st = NA if safe is None else OK if (planned or 0) <= safe else WARN
            msg = (f"OBS stream {planned:.1f} Mb/s" if planned else "") + (
                f" · safe up to {safe:.1f} Mb/s" if safe else " · run a speed test")
            adv = Gtk.Box(spacing=8)
            adv.append(dot(st))
            adv.append(label(msg.strip(" ·"), "row-sub", wrap=True, max_width_chars=70))
            c.append(adv)
        if len(online) < 2:
            c.append(label("Tip: plug in Ethernet or turn on USB tethering on your phone. "
                           "With two connections Stream Guard can fail over automatically "
                           "or send each platform through its own link.", "row-sub",
                           wrap=True, max_width_chars=80))

        c.append(label("INGEST SERVERS", "group-title"))
        for k, name, ms in ingests:
            row = Gtk.Box(spacing=12)
            row.append(label(name, "row-name", width_chars=10))
            bar = Gtk.ProgressBar(css_classes=["lat"], hexpand=True, valign=Gtk.Align.CENTER)
            if ms == "…":
                val, st = "checking…", NA
            elif ms is None:
                val, st = "unreachable", FAIL
                bar.set_fraction(1.0)
            else:
                val, st = f"{ms:.0f} ms", OK if ms < 250 else WARN
                bar.set_fraction(min(1.0, ms / 500))
            bar.add_css_class(STATUS_CLASS[st])
            row.append(bar)
            row.append(label(val, "num", xalign=1.0, width_chars=11))
            c.append(row)

    def _speed_test(self, dev):
        def done(d, up, down):
            if up is None:
                self.toast(f"Speed test on {d} failed")
            else:
                self.toast(f"{d}: upload {up:.1f} Mb/s, download {down or 0:.0f} Mb/s")
        self.engine.speed_test(dev, done)

    def _refresh_devices(self):
        e, card = self.engine, self.dev_card
        cfg = e.config
        video = sorted(set(e.devices.names("video")) | set(cfg["video_devices"]))
        key = (video, tuple(cfg["video_devices"]), tuple(sorted(e.devices.devices)))
        if not self._changed("dev", key):
            return
        clear(card.content)
        c = card.content
        if not video:
            c.append(label("No capture devices. Plug in your capture card.", "row-sub", wrap=True))
        for name in video:
            present = ("video", name) in e.devices.devices
            pinned = name in cfg["video_devices"]
            row = Gtk.Box(spacing=10)
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            txt.append(label(short(name, 30), "row-name"))
            txt.append(label((e.devices.describe("video", name) or "connected") if present
                             else "not connected", "row-sub"))
            row.append(txt)
            row.append(pill("WATCHED" if pinned and present else "MISSING" if pinned else "IDLE",
                            OK if pinned and present else FAIL if pinned else NA))
            c.append(row)

    def _refresh_checks(self, checks):
        card = self.check_card
        key = [(x.group, x.title, x.status, x.detail) for x in checks]
        if not self._changed("checks", key):
            return
        clear(card.badge)
        rated = [x for x in checks if x.status != NA]
        bad = sum(x.status != OK for x in rated)
        card.badge.append(pill("ALL CLEAR" if not bad else f"{bad} TO REVIEW", OK if not bad else
                               (FAIL if any(x.status == FAIL for x in checks) else WARN)))
        clear(self.check_cols)
        groups = {}
        for x in checks:
            groups.setdefault(x.group, []).append(x)
        # Three columns, filled greedily so they end at similar heights.
        cols = [Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14) for _ in range(3)]
        heights = [0, 0, 0]
        for gname, items in groups.items():
            i = heights.index(min(heights))
            heights[i] += len(items) + 1.5
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            cols[i].append(box)
            box.append(label(gname.upper(), "group-title"))
            for x in items:
                row = Gtk.Box(spacing=10)
                row.append(dot(x.status))
                row.append(label(x.title, "row-name", ellipsize=3, max_width_chars=16))
                row.append(label(x.detail, "check-detail", hexpand=True, xalign=1.0,
                                 ellipsize=3, max_width_chars=20))
                row.set_tooltip_text(x.detail)
                box.append(row)
        for col in cols:
            self.check_cols.append(col)

    # ------------------------------------------------------------- actions
    def _on_live_clicked(self, _btn):
        e = self.engine
        if e.live:
            self._confirm_end()
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
        e = self.engine
        e.go_live()
        if e.config["control_obs"]:
            if e.obs.state == "connected":
                e.start_obs(on_error=self._obs_error)
                self.toast("Starting the stream in OBS…")
            else:
                self.toast("OBS isn't connected: watching only. Start the stream in OBS yourself.")
        else:
            self.toast("You're on air. Stream Guard is watching.")

    def _obs_error(self, message):
        dlg = Adw.AlertDialog(heading="OBS couldn't start", body=message +
                              "\n\nCheck OBS → Help → Log Files for details.")
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def _confirm_end(self):
        e = self.engine
        can_stop = e.config["control_obs"] and e.obs.state == "connected" and (
            e.obs.streaming or e.obs.recording)
        dlg = Adw.AlertDialog(
            heading="End the stream?",
            body="This stops the stream in OBS for every platform, ends the session here "
                 "and saves a report." if can_stop else
                 "Stream Guard stops watching and saves a report.")
        dlg.add_response("cancel", "Keep streaming")
        if can_stop:
            dlg.add_response("watch", "Only stop watching")
            dlg.add_response("stop", "End stream")
            dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dlg.add_response("watch", "End session")
            dlg.set_response_appearance("watch", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: r in ("watch", "stop") and self._end(r == "stop"))
        dlg.present(self)

    def _obs_toggle(self, what):
        o = self.engine.obs
        if what == "stream":
            if o.streaming:
                if self.engine.live:
                    self._confirm_end()
                else:
                    o.stop_stream(err=self._obs_error)
            elif self.engine.live:
                o.start_stream(err=self._obs_error)
            else:
                self._on_live_clicked(None)  # runs the ready check, then starts OBS
        else:
            (o.stop_record if o.recording else o.start_record)(err=self._obs_error)

    def _end(self, stop_obs=False):
        path = self.engine.end_live(stop_obs=stop_obs, on_error=self._obs_error)
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
