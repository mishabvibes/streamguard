"""Alerts: desktop notification, optional sound, optional phone push (ntfy).

Each alert has a key so a condition that stays true only alerts once, and is
"cleared" when the condition goes away.
"""

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass

import dbus
import gi

gi.require_version("Soup", "3.0")
from gi.repository import GLib, Soup  # noqa: E402

log = logging.getLogger(__name__)

INFO, WARN, CRITICAL = "info", "warn", "critical"
SOUNDS = {
    WARN: "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
    CRITICAL: "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
}
NTFY_PRIORITY = {INFO: "default", WARN: "high", CRITICAL: "urgent"}


@dataclass
class Alert:
    key: str
    severity: str
    title: str
    body: str
    t: float
    wall: float
    cleared: float = None


class Alerts:
    def __init__(self, config, on_change):
        self.config = config
        self.on_change = on_change
        self.active = {}      # key -> Alert
        self.history = []     # every alert raised, for the session report
        self.session = Soup.Session()
        self.muted = False    # quiet while not live (checklist mode)

    def raise_(self, key, severity, title, body=""):
        if key in self.active:
            return
        a = Alert(key, severity, title, body, time.monotonic(), time.time())
        self.active[key] = a
        self.history.append(a)
        log.info("ALERT %s: %s %s", severity, title, body)
        if not self.muted:
            self._notify(a)
            self._sound(a)
            self._push(a)
        self.on_change()

    def clear(self, key, note=None):
        a = self.active.pop(key, None)
        if a is None:
            return
        a.cleared = time.time()
        if note and not self.muted:
            self._push(Alert(key + ":ok", INFO, note, "", time.monotonic(), time.time()))
        self.on_change()

    def dismiss(self, key):
        """User acknowledged; keep it in history but drop it from the banner."""
        a = self.active.pop(key, None)
        if a:
            a.cleared = a.cleared or time.time()
            self.on_change()

    # ----------------------------------------------------------- channels
    def _notify(self, a):
        try:
            n = dbus.SessionBus().get_object("org.freedesktop.Notifications",
                                             "/org/freedesktop/Notifications")
            hints = {"urgency": dbus.Byte(2 if a.severity == CRITICAL else 1)}
            n.Notify("Stream Guard", dbus.UInt32(0), "dialog-warning-symbolic", a.title, a.body,
                     dbus.Array([], signature="s"), dbus.Dictionary(hints, signature="sv"),
                     0 if a.severity == CRITICAL else 8000,
                     dbus_interface="org.freedesktop.Notifications")
        except dbus.DBusException as e:
            log.debug("notification failed: %s", e)

    def _sound(self, a):
        # Off by default: on the default output the alarm could end up in the
        # stream if OBS captures desktop audio. Users pick e.g. their headphones.
        snd = SOUNDS.get(a.severity)
        if not (self.config["alert_sound"] and snd and shutil.which("paplay")):
            return
        cmd = ["paplay", snd]
        if self.config["alert_sink"]:
            cmd[1:1] = ["--device", self.config["alert_sink"]]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _push(self, a):
        topic = (self.config["ntfy_topic"] or "").strip()
        if not topic:
            return
        url = self.config["ntfy_server"].rstrip("/") + "/" + topic
        msg = Soup.Message.new("POST", url)
        if msg is None:
            return
        h = msg.get_request_headers()
        h.append("Title", a.title.encode("ascii", "replace").decode())
        h.append("Priority", NTFY_PRIORITY[a.severity])
        h.append("Tags", {"critical": "rotating_light", "warn": "warning"}.get(a.severity, "white_check_mark"))
        msg.set_request_body_from_bytes("text/plain", GLib.Bytes.new((a.body or a.title).encode()))
        self.session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, None,
                                         lambda s, r: self._push_done(s, r))

    @staticmethod
    def _push_done(session, res):
        try:
            session.send_and_read_finish(res)
        except GLib.Error as e:
            log.warning("ntfy push failed: %s", e.message)

    def test(self):
        a = Alert("test", WARN, "Stream Guard test alert",
                  "If you can see this, alerts work.", time.monotonic(), time.time())
        self._notify(a)
        self._sound(a)
        self._push(a)
