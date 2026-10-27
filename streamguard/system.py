"""Network, system and power readings. All reads are cheap sysfs/procfs files,
except the GPU (one long-running nvidia-smi) and ingest probes (TCP connects)."""

import glob
import logging
import os
import re
import shutil
import subprocess
import time

import dbus
from gi.repository import Gio, GLib

log = logging.getLogger(__name__)


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ------------------------------------------------------------------ network
def default_iface(route_text=None):
    """Interface of the default route with the lowest metric (from /proc/net/route)."""
    text = route_text if route_text is not None else (read("/proc/net/route") or "")
    best = None
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 7 and f[1] == "00000000":
            metric = int(f[6])
            if best is None or metric < best[1]:
                best = (f[0], metric)
    return best[0] if best else None


def iface_kind(iface, sys_root="/sys"):
    base = os.path.join(sys_root, "class/net", iface)
    if os.path.exists(os.path.join(base, "wireless")) or os.path.exists(
            os.path.join(base, "phy80211")):
        return "wifi"
    driver = os.path.basename(os.path.realpath(os.path.join(base, "device/driver")))
    if driver in ("rndis_host", "cdc_ether", "cdc_ncm", "ipheth", "cdc_mbim", "qmi_wwan"):
        return "usb-tether"
    return "ethernet"


def wifi_dbm(iface, wireless_text=None):
    text = wireless_text if wireless_text is not None else (read("/proc/net/wireless") or "")
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == iface:
            f = rest.split()
            try:
                return float(f[2].rstrip("."))
            except (IndexError, ValueError):
                return None
    return None


class Network:
    def __init__(self):
        self.iface = None
        self.kind = None
        self.dbm = None
        self.link_mbps = None
        self._last = None
        self.tx_bps = None
        self.rx_bps = None

    def sample(self):
        iface = default_iface()
        if iface != self.iface:
            self._last = None
        self.iface = iface
        if not iface:
            self.kind = self.dbm = self.link_mbps = self.tx_bps = self.rx_bps = None
            return
        self.kind = iface_kind(iface)
        self.dbm = wifi_dbm(iface) if self.kind == "wifi" else None
        speed = read(f"/sys/class/net/{iface}/speed")
        self.link_mbps = int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None
        tx = read(f"/sys/class/net/{iface}/statistics/tx_bytes")
        rx = read(f"/sys/class/net/{iface}/statistics/rx_bytes")
        now = time.monotonic()
        if tx and rx:
            cur = (now, int(tx), int(rx))
            if self._last:
                dt = cur[0] - self._last[0]
                if dt > 0:
                    self.tx_bps = max(0, cur[1] - self._last[1]) * 8 / dt
                    self.rx_bps = max(0, cur[2] - self._last[2]) * 8 / dt
            self._last = cur


class IngestProber:
    """Measures TCP connect time to each platform's ingest server."""

    def __init__(self, on_result):
        self.on_result = on_result
        self.results = {}

    def probe(self, key, host, port):
        client = Gio.SocketClient.new()
        client.set_timeout(4)
        t0 = time.monotonic()

        def done(c, res):
            try:
                conn = c.connect_to_host_finish(res)
                ms = (time.monotonic() - t0) * 1000
                conn.close(None)
            except GLib.Error:
                ms = None
            self.results[key] = ms
            self.on_result(key, ms)

        client.connect_to_host_async(host, port, None, done)


# ------------------------------------------------------------------- system
class CpuLoad:
    def __init__(self):
        self._last = None

    def sample(self):
        f = (read("/proc/stat") or "").splitlines()[0].split()[1:]
        vals = [int(x) for x in f]
        idle, total = vals[3] + vals[4], sum(vals)
        res = None
        if self._last:
            di, dt = idle - self._last[0], total - self._last[1]
            res = 100 * (1 - di / dt) if dt > 0 else None
        self._last = (idle, total)
        return res


def cpu_temp():
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read(os.path.join(h, "name")) in ("k10temp", "coretemp", "zenpower"):
            v = read(os.path.join(h, "temp1_input"))
            if v:
                return int(v) / 1000
    return None


def power_state():
    on_ac, battery = None, None
    for d in glob.glob("/sys/class/power_supply/*"):
        t = read(os.path.join(d, "type"))
        if t == "Mains":
            on_ac = read(os.path.join(d, "online")) == "1"
        elif t == "Battery" and read(os.path.join(d, "scope")) != "Device" and battery is None:
            c = read(os.path.join(d, "capacity"))
            battery = int(c) if c and c.isdigit() else None
    return on_ac, battery


def sample_rate():
    """PipeWire's clock rate (what every stream is resampled to)."""
    try:
        out = subprocess.run(["pw-metadata", "-n", "settings", "0", "clock.force-rate"],
                             capture_output=True, text=True, timeout=2).stdout
        forced = [int(x) for x in re.findall(r"value:'(\d+)'", out)]
        if forced and forced[0]:
            return forced[0]
        out = subprocess.run(["pw-metadata", "-n", "settings", "0", "clock.rate"],
                             capture_output=True, text=True, timeout=2).stdout
        rates = [int(x) for x in re.findall(r"value:'(\d+)'", out)]
        return rates[0] if rates else None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Gpu:
    """NVIDIA stats from one long-running `nvidia-smi -lms`, only while started."""

    QUERY = "temperature.gpu,utilization.gpu,utilization.encoder,power.draw"

    def __init__(self):
        self.proc = None
        self.temp = self.util = self.enc = self.watts = None
        self.available = shutil.which("nvidia-smi") is not None

    def start(self):
        if self.proc or not self.available:
            return
        try:
            self.proc = subprocess.Popen(
                ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits",
                 "-lms", "2000"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self.available = False
            return
        GLib.io_add_watch(self.proc.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP, self._on_line)

    def _on_line(self, fd, cond):
        if cond & GLib.IO_HUP or self.proc is None:
            return False
        line = self.proc.stdout.readline()
        parts = [p.strip() for p in line.split(",")]
        num = lambda x: float(x) if x.replace(".", "", 1).isdigit() else None
        if len(parts) >= 4:
            self.temp, self.util, self.enc, self.watts = (num(p) for p in parts[:4])
        return True

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc = None
            self.temp = self.util = self.enc = self.watts = None


def disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


# ------------------------------------------------------- desktop integration
class SleepInhibitor:
    """Blocks suspend and idle (screen lock) via logind while live."""

    def __init__(self):
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            return True
        try:
            mgr = dbus.SystemBus().get_object("org.freedesktop.login1", "/org/freedesktop/login1")
            fd = mgr.Inhibit("sleep:idle", "Stream Guard", "Live stream in progress", "block",
                             dbus_interface="org.freedesktop.login1.Manager")
            self.fd = fd.take()
            return True
        except dbus.DBusException as e:
            log.warning("could not inhibit sleep: %s", e)
            return False

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Lpowerd:
    """Optional link to lpowerd (https://github.com/mishabvibes/lpowerd)."""

    def __init__(self):
        self.bus = dbus.SystemBus()

    def _iface(self):
        obj = self.bus.get_object("io.github.lpowerd", "/io/github/lpowerd", introspect=False)
        return dbus.Interface(obj, "io.github.lpowerd.Manager1")

    def profile(self):
        try:
            import json
            return json.loads(str(self._iface().GetStatus(timeout=2)))["profile"]["active"]
        except (dbus.DBusException, KeyError, ValueError):
            return None

    def set_profile(self, name):
        try:
            self._iface().SetProfile(name, timeout=10)
            return True
        except dbus.DBusException as e:
            log.warning("lpowerd SetProfile(%s) failed: %s", name, e)
            return False
