"""Minimal OBS WebSocket v5 client on libsoup3 (built into OBS 28+).

Reads the port and password from OBS's own config (native or Flatpak), so
the only setup is ticking "Enable WebSocket server" in OBS once.
"""

import base64
import configparser
import hashlib
import json
import logging
import os
import time

import gi

gi.require_version("Soup", "3.0")
from gi.repository import GLib, Soup  # noqa: E402

from .rules import RateTracker  # noqa: E402

log = logging.getLogger(__name__)

OBS_DIRS = (
    "~/.var/app/com.obsproject.Studio/config/obs-studio",  # Flatpak
    "~/.config/obs-studio",
)
EVENTS_GENERAL, EVENTS_OUTPUTS = 1 << 0, 1 << 6


def obs_dir():
    for d in OBS_DIRS:
        p = os.path.expanduser(d)
        if os.path.isdir(p):
            return p
    return None


def websocket_settings():
    """(enabled, port, password) from obs-websocket's config, or None."""
    d = obs_dir()
    if not d:
        return None
    try:
        with open(os.path.join(d, "plugin_config/obs-websocket/config.json")) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    pw = c.get("server_password") if c.get("auth_required") else None
    return bool(c.get("server_enabled")), int(c.get("server_port", 4455)), pw


def recording_path():
    """Recording folder of the active OBS profile."""
    d = obs_dir()
    if not d:
        return None
    g = configparser.ConfigParser(strict=False, interpolation=None)
    g.read(os.path.join(d, "user.ini"))
    g.read(os.path.join(d, "global.ini"))
    prof = g.get("Basic", "ProfileDir", fallback=None)
    if not prof:
        return None
    b = configparser.ConfigParser(strict=False, interpolation=None)
    b.read(os.path.join(d, "basic/profiles", prof, "basic.ini"))
    if b.get("Output", "Mode", fallback="Simple") == "Advanced":
        path = b.get("AdvOut", "RecFilePath", fallback=None)
    else:
        path = b.get("SimpleOutput", "FilePath", fallback=None)
    if path and path.startswith("/var/home/"):
        path = path[4:]  # Flatpak path prefix on some systems
    return path or os.path.expanduser("~/Videos")


def auth_string(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class Output:
    """One OBS output (main stream, recording, or a multistream plugin target)."""

    def __init__(self, name, kind):
        self.name = name
        self.kind = kind
        self.active = False
        self.reconnecting = False
        self.congestion = 0.0
        self.bytes = 0
        self.skipped = 0
        self.total = 0
        self.rates = RateTracker(window=30)

    @property
    def kbps(self):
        r = self.rates.rate("bytes", span=5)
        return None if r is None else r * 8 / 1000

    @property
    def drop_pct(self):
        r = self.rates.ratio("skipped", "total")
        return None if r is None else r * 100


class OBSClient:
    """Connects, identifies, then polls stats every 2 s while running."""

    POLL_MS = 2000

    def __init__(self, on_update, on_event, port=None, password=None):
        self.on_update = on_update      # () -> None, after each poll
        self.on_event = on_event        # (event_type, data)
        self.port_override = port
        self.password_override = password
        self.session = Soup.Session()
        self.conn = None
        self.state = "offline"          # disabled | offline | connecting | connected
        self.error = None
        self.stats = {}
        self.outputs = {}
        self.streaming = False
        self.recording = False
        self._pending = {}
        self._next_id = 0
        self._poll = None
        self._retry = None
        self.running = False

    # ---------------------------------------------------------- lifecycle
    def start(self):
        self.running = True
        self._connect()

    def stop(self):
        self.running = False
        for src in ("_poll", "_retry"):
            if getattr(self, src):
                GLib.source_remove(getattr(self, src))
                setattr(self, src, None)
        if self.conn is not None:
            self.conn.close(Soup.WebsocketCloseCode.NORMAL, None)
            self.conn = None
        self.state = "offline"

    def _schedule_retry(self):
        if self.running and self._retry is None:
            self._retry = GLib.timeout_add_seconds(5, self._retry_cb)

    def _retry_cb(self):
        self._retry = None
        self._connect()
        return False

    def _connect(self):
        if not self.running or self.conn is not None:
            return
        cfg = websocket_settings()
        if cfg is None and self.port_override is None:
            self.state, self.error = "offline", "OBS is not installed or never started"
            self._schedule_retry()
            return
        enabled, port, pw = cfg or (True, 4455, None)
        if not enabled and self.port_override is None:
            self.state = "disabled"
            self.on_update()
            self._schedule_retry()
            return
        self.password = self.password_override or pw
        port = self.port_override or port
        self.state = "connecting"
        msg = Soup.Message.new("GET", f"ws://127.0.0.1:{port}")
        self.session.websocket_connect_async(msg, None, ["obswebsocket.json"],
                                             GLib.PRIORITY_DEFAULT, None, self._on_connected)

    def _on_connected(self, session, res):
        try:
            self.conn = session.websocket_connect_finish(res)
        except GLib.Error:
            self.conn = None
            self.state = "offline"
            self.on_update()
            self._schedule_retry()
            return
        self.conn.set_keepalive_interval(10)
        self.conn.connect("message", self._on_message)
        self.conn.connect("closed", self._on_closed)

    def _on_closed(self, conn):
        log.info("OBS connection closed")
        self.conn = None
        self.state = "offline"
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = None
        self.streaming = self.recording = False
        self.on_update()
        self._schedule_retry()

    # ----------------------------------------------------------- protocol
    def _send(self, op, d):
        if self.conn is not None:
            self.conn.send_text(json.dumps({"op": op, "d": d}))

    def request(self, rtype, data=None, cb=None):
        self._next_id += 1
        rid = str(self._next_id)
        if cb:
            self._pending[rid] = cb
        d = {"requestType": rtype, "requestId": rid}
        if data:
            d["requestData"] = data
        self._send(6, d)

    def _on_message(self, conn, typ, data):
        try:
            msg = json.loads(data.get_data().decode())
        except ValueError:
            return
        op, d = msg.get("op"), msg.get("d", {})
        if op == 0:  # Hello
            ident = {"rpcVersion": 1, "eventSubscriptions": EVENTS_GENERAL | EVENTS_OUTPUTS}
            auth = d.get("authentication")
            if auth:
                if not self.password:
                    self.error = "OBS asks for a WebSocket password; set it in Settings"
                    conn.close(Soup.WebsocketCloseCode.NORMAL, None)
                    return
                ident["authentication"] = auth_string(self.password, auth["salt"],
                                                      auth["challenge"])
            self._send(1, ident)
        elif op == 2:  # Identified
            self.state, self.error = "connected", None
            log.info("connected to OBS")
            self._poll_now()
            self._poll = GLib.timeout_add(self.POLL_MS, self._poll_now)
        elif op == 5:  # Event
            self._on_obs_event(d.get("eventType"), d.get("eventData") or {})
        elif op == 7:  # RequestResponse
            cb = self._pending.pop(d.get("requestId"), None)
            if cb and d.get("requestStatus", {}).get("result"):
                cb(d.get("responseData") or {})

    def _on_obs_event(self, etype, data):
        if etype == "StreamStateChanged":
            self.streaming = bool(data.get("outputActive"))
        elif etype == "RecordStateChanged":
            self.recording = bool(data.get("outputActive"))
        self.on_event(etype, data)

    # --------------------------------------------------------------- poll
    def _poll_now(self):
        if self.state != "connected":
            return False
        self.request("GetStats", cb=self._on_stats)
        self.request("GetOutputList", cb=self._on_outputs)
        return True

    def _on_stats(self, d):
        self.stats = d
        self.on_update()

    def _on_outputs(self, d):
        seen = set()
        for o in d.get("outputs", []):
            name, kind = o.get("outputName"), o.get("outputKind", "")
            if not name or "virtualcam" in kind or "replay" in kind:
                continue
            seen.add(name)
            if name not in self.outputs:
                self.outputs[name] = Output(name, kind)
            self.request("GetOutputStatus", {"outputName": name},
                         cb=lambda r, n=name: self._on_output_status(n, r))
        for gone in set(self.outputs) - seen:
            del self.outputs[gone]

    def _on_output_status(self, name, d):
        o = self.outputs.get(name)
        if o is None:
            return
        o.active = bool(d.get("outputActive"))
        o.reconnecting = bool(d.get("outputReconnecting"))
        o.congestion = float(d.get("outputCongestion") or 0)
        o.bytes = int(d.get("outputBytes") or 0)
        o.skipped = int(d.get("outputSkippedFrames") or 0)
        o.total = int(d.get("outputTotalFrames") or 0)
        if o.active:
            o.rates.add(time.monotonic(), bytes=o.bytes, skipped=o.skipped, total=o.total)
        else:
            o.rates = RateTracker(window=30)
        self.streaming = any(x.active and is_stream(x) for x in self.outputs.values())
        self.recording = any(x.active and not is_stream(x) for x in self.outputs.values())
        self.on_update()


STREAM_KINDS = {"rtmp_output", "whip_output", "ffmpeg_mpegts_muxer", "ftl_output"}


def is_stream(output):
    """Network outputs (RTMP, WHIP, SRT/RIST via mpegts) vs. recordings."""
    return output.kind in STREAM_KINDS or output.name.endswith("stream")
