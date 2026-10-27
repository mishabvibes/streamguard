"""Custom-drawn widgets (GSK, GPU-rendered): sparkline, readiness ring, LED meters."""

import math

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402


def rgba(h, a=1.0):
    c = Gdk.RGBA()
    c.parse(h)
    c.alpha = a
    return c


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def stroke(snap, path, width, color, cap=Gsk.LineCap.ROUND):
    st = Gsk.Stroke.new(width)
    st.set_line_cap(cap)
    st.set_line_join(Gsk.LineJoin.ROUND)
    snap.append_stroke(path, st, color)


class Sparkline(Gtk.Widget):
    """Area sparkline of recent values. Fixed scale optional (e.g. 0..100 °C)."""

    def __init__(self, color="#22d3ee", height=38, lo=None, hi=None, min_span=1.0):
        super().__init__()
        self.values = []
        self.color = color
        self.height = height
        self.lo, self.hi, self.min_span = lo, hi, min_span

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 60, 160, -1, -1
        return self.height, self.height, -1, -1

    def set_values(self, values, color=None):
        values = list(values)
        if values == self.values and (color is None or color == self.color):
            return
        self.values = values
        if color:
            self.color = color
        self.queue_draw()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        vals = self.values
        # baseline
        snap.append_color(rgba("#ffffff", 0.06), rect(0, h - 1, w, 1))
        if len(vals) < 2:
            return
        lo = self.lo if self.lo is not None else min(vals)
        hi = self.hi if self.hi is not None else max(vals)
        if hi - lo < self.min_span:
            hi = lo + self.min_span
        pad = 3
        n = len(vals)
        pts = [(w * i / (n - 1), pad + (h - 2 * pad) * (1 - (v - lo) / (hi - lo)))
               for i, v in enumerate(vals)]
        area = Gsk.PathBuilder.new()
        area.move_to(0, h)
        for x, y in pts:
            area.line_to(x, y)
        area.line_to(w, h)
        area.close()
        snap.push_fill(area.to_path(), Gsk.FillRule.WINDING)
        start, end = Graphene.Point().init(0, 0), Graphene.Point().init(0, h)
        stops = [Gsk.ColorStop(), Gsk.ColorStop()]
        stops[0].offset, stops[0].color = 0.0, rgba(self.color, 0.28)
        stops[1].offset, stops[1].color = 1.0, rgba(self.color, 0.0)
        snap.append_linear_gradient(rect(0, 0, w, h), start, end, stops)
        snap.pop()
        line = Gsk.PathBuilder.new()
        line.move_to(*pts[0])
        for x, y in pts[1:]:
            line.line_to(x, y)
        stroke(snap, line.to_path(), 1.8, rgba(self.color))
        dot = Gsk.PathBuilder.new()
        dot.add_circle(Graphene.Point().init(*pts[-1]), 2.6)
        snap.append_fill(dot.to_path(), Gsk.FillRule.WINDING, rgba(self.color))


class Ring(Gtk.Widget):
    """Readiness ring: passed / total checks."""

    SIZE = 112

    def __init__(self):
        super().__init__()
        self.frac = 0.0
        self.color = "#22d3ee"

    def do_measure(self, orientation, for_size):
        return self.SIZE, self.SIZE, -1, -1

    def set_state(self, frac, color):
        if (frac, color) != (self.frac, self.color):
            self.frac, self.color = frac, color
            self.queue_draw()

    def _arc(self, cx, cy, r, frac):
        b = Gsk.PathBuilder.new()
        n = max(2, int(96 * frac))
        for i in range(n + 1):
            a = -math.pi / 2 + 2 * math.pi * frac * i / n
            (b.move_to if i == 0 else b.line_to)(cx + r * math.cos(a), cy + r * math.sin(a))
        return b.to_path()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        cx, cy, r = w / 2, h / 2, min(w, h) / 2 - 8
        track = Gsk.PathBuilder.new()
        track.add_circle(Graphene.Point().init(cx, cy), r)
        stroke(snap, track.to_path(), 8, rgba("#ffffff", 0.07))
        if self.frac > 0.001:
            stroke(snap, self._arc(cx, cy, r, min(self.frac, 0.9999)), 8, rgba(self.color))


class LedMeter(Gtk.Widget):
    """Vertical LED-segment meters, one column per channel, -60..0 dBFS."""

    SEGMENTS = 30
    LO = -60.0
    HEIGHT = 170

    def __init__(self):
        super().__init__()
        self.peak = []
        self.hold = []

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            n = max(2, len(self.peak))
            return n * 22 + (n - 1) * 8, n * 22 + (n - 1) * 8, -1, -1
        return self.HEIGHT, self.HEIGHT, -1, -1

    def set_levels(self, peak):
        if len(self.hold) != len(peak):
            self.hold = list(peak)
        self.hold = [max(p, h - 0.8) for p, h in zip(peak, self.hold)]
        resize = len(peak) != len(self.peak)
        self.peak = list(peak)
        self.queue_resize() if resize else self.queue_draw()

    def _seg(self, db):
        return int(max(0.0, min(1.0, (db - self.LO) / -self.LO)) * self.SEGMENTS + 0.5)

    @staticmethod
    def _color(i, n):
        db = -60 + 60 * (i + 1) / n
        return "#ef4444" if db > -6 else "#f59e0b" if db > -18 else "#22c55e"

    def do_snapshot(self, snap):
        h = self.get_height()
        n = self.SEGMENTS
        gap = 2
        seg_h = (h - gap * (n - 1)) / n
        for ch in range(max(2, len(self.peak))):
            x = ch * 30
            lit = self._seg(self.peak[ch]) if ch < len(self.peak) else 0
            hold = self._seg(self.hold[ch]) - 1 if ch < len(self.hold) else -1
            for i in range(n):
                y = h - (i + 1) * seg_h - i * gap
                col = self._color(i, n)
                on = i < lit or i == hold
                rr = Gsk.RoundedRect()
                rr.init_from_rect(rect(x, y, 22, seg_h), 1.5)
                snap.push_rounded_clip(rr)
                snap.append_color(rgba(col, 1.0 if on else 0.13), rect(x, y, 22, seg_h))
                snap.pop()
