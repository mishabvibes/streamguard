"""Multi-link networking: per-link health, failover, speed tests, platform split.

Everything goes through NetworkManager as the desktop user (polkit allows
`network-control` for the active session), and only with `nmcli device
modify`, which changes the *running* config: nothing is written to the
saved connection profiles, and `restore()` puts it all back.
"""

import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

from gi.repository import GLib

log = logging.getLogger(__name__)

PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443))   # anycast IPs: no DNS needed
FAIL_LIMIT = 3            # consecutive failed probes before a link counts as down
FAILOVER_METRIC = 5       # lower than any NetworkManager default (ethernet 100, wifi 600)
SPEED_UP_URL = "https://speed.cloudflare.com/__up"
SPEED_DOWN_URL = "https://speed.cloudflare.com/__down?bytes=25000000"  # larger is refused
SPEED_SECONDS = 7
SPEED_UPLOAD_BYTES = 30_000_000


@dataclass
class Link:
    dev: str
    kind: str                  # ethernet | wifi | usb-tether | other
    gateway: str = None
    metric: int = 0            # current default-route metric
    src: str = None
    rtt_ms: float = None
    fails: int = 0
    healthy: bool = None       # None until the first probe finishes
    up_mbps: float = None
    down_mbps: float = None
    testing: bool = False
    platforms: list = field(default_factory=list)

    @property
    def label(self):
        return {"ethernet": "Ethernet", "wifi": "Wi-Fi", "usb-tether": "Phone (USB)"}.get(
            self.kind, self.dev)


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def default_routes(text=None):
    """[{dev, gateway, metric, prefsrc}] from `ip -j -4 route show default`."""
    if text is None:
        _, text, _ = run(["ip", "-j", "-4", "route", "show", "default"])
    try:
        routes = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for r in routes:
        if r.get("dev"):
            out.append({"dev": r["dev"], "gateway": r.get("gateway"),
                        "metric": int(r.get("metric", 0)), "prefsrc": r.get("prefsrc")})
    return out


def probe(dev, targets=PROBE_TARGETS, timeout=3.0):
    """TCP connect through one interface only. Returns RTT in ms or None."""
    for host, port in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Unprivileged since Linux 5.7.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, dev.encode())
            s.settimeout(timeout)
            t0 = time.monotonic()
            s.connect((host, port))
            return (time.monotonic() - t0) * 1000
        except OSError:
            continue
        finally:
            s.close()
    return None


def choose_failover(links, primary_dev):
    """Pure policy: which device should carry the default route?

    Returns the dev to promote, or None to leave routing alone.
    """
    by_dev = {l.dev: l for l in links}
    primary = by_dev.get(primary_dev)
    if primary is None or primary.fails < FAIL_LIMIT:
        return None
    backups = [l for l in links if l.dev != primary_dev and l.healthy]
    if not backups:
        return None
    # Prefer wired, then the fastest measured upload, then lowest latency.
    order = {"ethernet": 0, "usb-tether": 1, "wifi": 2}
    backups.sort(key=lambda l: (order.get(l.kind, 3), -(l.up_mbps or 0), l.rtt_ms or 9e9))
    return backups[0].dev


def safe_bitrate_mbps(links):
    """70 % of the best measured upload: headroom for bursts and other traffic."""
    ups = [l.up_mbps for l in links if l.up_mbps]
    return round(max(ups) * 0.7, 1) if ups else None


class LinkManager:
    PROBE_S = 5

    def __init__(self, kind_of, on_change, on_event):
        self.kind_of = kind_of          # dev -> kind (from system.iface_kind)
        self.on_change = on_change
        self.on_event = on_event        # (severity, title, body)
        self.links = {}
        self.failover_enabled = False   # turned on only while live
        self.promoted = {}              # dev -> original metric we changed
        self.split_routes = {}          # dev -> set of "ip/32 gw" we added
        self._src = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- polling
    def start(self):
        if self._src is None:
            self.refresh()
            self._src = GLib.timeout_add_seconds(self.PROBE_S, self._tick)

    def stop(self):
        if self._src:
            GLib.source_remove(self._src)
            self._src = None

    def refresh(self):
        routes = default_routes()
        seen = set()
        for r in routes:
            dev = r["dev"]
            seen.add(dev)
            l = self.links.get(dev) or Link(dev, self.kind_of(dev))
            l.gateway, l.metric, l.src = r["gateway"], r["metric"], r["prefsrc"]
            self.links[dev] = l
        for gone in set(self.links) - seen:
            if self.failover_enabled:
                self.on_event("warn", f"{self.links[gone].label} disconnected", gone)
            del self.links[gone]

    def primary(self):
        live = sorted(self.links.values(), key=lambda l: l.metric)
        return live[0].dev if live else None

    def _tick(self):
        self.refresh()
        for l in list(self.links.values()):
            threading.Thread(target=self._probe_one, args=(l.dev,), daemon=True).start()
        return True

    def _probe_one(self, dev):
        ms = probe(dev)
        GLib.idle_add(self._probe_done, dev, ms)

    def _probe_done(self, dev, ms):
        l = self.links.get(dev)
        if l is None:
            return False
        was = l.healthy
        l.rtt_ms = ms
        l.fails = 0 if ms is not None else l.fails + 1
        l.healthy = ms is not None or l.fails < FAIL_LIMIT
        if was and not l.healthy and self.failover_enabled:
            self.on_event("critical", f"{l.label} has no internet",
                          f"{dev}: {FAIL_LIMIT} checks in a row failed.")
        elif was is False and l.healthy and self.failover_enabled:
            self.on_event("info", f"{l.label} is back online", dev)
        if self.failover_enabled:
            self._maybe_failover()
        self.on_change()
        return False

    # ------------------------------------------------------------ failover
    def _maybe_failover(self):
        target = choose_failover(list(self.links.values()), self.primary())
        if not target:
            return
        old = self.links[self.primary()]
        new = self.links[target]
        if self._set_metric(target, FAILOVER_METRIC):
            self.promoted.setdefault(target, new.metric)
            self.on_event("critical", f"Switched to {new.label}",
                          f"{old.label} lost internet. OBS reconnects through {new.label}.")
            self.refresh()

    def _set_metric(self, dev, metric):
        rc, _, err = run(["nmcli", "device", "modify", dev, "ipv4.route-metric", str(metric)])
        if rc != 0:
            log.warning("could not change route metric on %s: %s", dev, err)
            self.on_event("warn", "Couldn't switch network", err)
        return rc == 0

    # ---------------------------------------------------------- split routes
    def apply_split(self, assignments, resolve):
        """Route each platform's ingest IPs through its assigned link.

        assignments: {platform_key: dev}; resolve: platform_key -> [ipv4].
        Additive while live: CDNs hand out different IPs on every lookup, so
        IPs seen earlier stay routed (OBS may have resolved one of them).
        """
        wanted = {dev: set(r) for dev, r in self.split_routes.items()}
        for key, dev in assignments.items():
            l = self.links.get(dev)
            if not dev or l is None:
                continue
            for ip in resolve(key):
                route = f"{ip}/32 {l.gateway}" if l.gateway else f"{ip}/32"
                wanted.setdefault(dev, set()).add(route)
        for dev in wanted:
            have, want = self.split_routes.get(dev, set()), wanted[dev]
            args = []
            for r in sorted(want - have):
                args += ["+ipv4.routes", r]
            if not args:
                continue
            rc, _, err = run(["nmcli", "device", "modify", dev] + args)
            if rc == 0:
                self.split_routes[dev] = want
            else:
                log.warning("split routes on %s failed: %s", dev, err)
                self.on_event("warn", "Couldn't route a platform through its link", err)
        for l in self.links.values():
            l.platforms = [k for k, d in assignments.items() if d == l.dev]

    def restore(self):
        """Undo every runtime change: `reapply` loads the saved profile again."""
        for dev in set(self.split_routes) | set(self.promoted):
            if dev in self.links:
                rc, _, err = run(["nmcli", "device", "reapply", dev])
                if rc != 0:
                    log.warning("could not restore %s: %s", dev, err)
        self.split_routes = {}
        self.promoted = {}
        self.refresh()

    # ----------------------------------------------------------- speed test
    def speed_test(self, dev, done):
        l = self.links.get(dev)
        if l is None or l.testing:
            return
        l.testing = True
        self.on_change()
        threading.Thread(target=self._speed_worker, args=(dev, done), daemon=True).start()

    def _speed_worker(self, dev, done):
        up = down = None
        with tempfile.NamedTemporaryFile(prefix="sg-speed-") as f:
            f.write(os.urandom(SPEED_UPLOAD_BYTES))
            f.flush()
            rc, out, _ = run(["curl", "-s", "-o", "/dev/null", "--interface", dev,
                              "--max-time", str(SPEED_SECONDS), "-w", "%{http_code} %{speed_upload}",
                              "--data-binary", f"@{f.name}", SPEED_UP_URL],
                             timeout=SPEED_SECONDS + 5)
            up = _mbps(out)
        rc, out, _ = run(["curl", "-s", "-o", "/dev/null", "--interface", dev,
                          "--max-time", str(SPEED_SECONDS), "-w", "%{http_code} %{speed_download}",
                          SPEED_DOWN_URL], timeout=SPEED_SECONDS + 5)
        down = _mbps(out)
        GLib.idle_add(self._speed_done, dev, up, down, done)

    def _speed_done(self, dev, up, down, done):
        l = self.links.get(dev)
        if l:
            l.testing = False
            l.up_mbps, l.down_mbps = up, down
        self.on_change()
        if done:
            done(dev, up, down)
        return False


def _mbps(curl_out):
    """Parse curl's "<http_code> <bytes/s>"; a timeout (code 000) still has a valid speed."""
    try:
        code, speed = curl_out.split()
        v = float(speed)
    except ValueError:
        return None
    if code not in ("200", "100", "000"):  # 100/000: cut off by --max-time mid-transfer
        return None
    return round(v * 8 / 1e6, 1) if v > 0 else None


def resolve_ipv4(host):
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def link_for_ip(ip):
    """Which interface the kernel uses to reach `ip` right now."""
    rc, out, _ = run(["ip", "-j", "route", "get", ip], timeout=3)
    try:
        return json.loads(out)[0].get("dev") if rc == 0 else None
    except (ValueError, IndexError):
        return None


def obs_connections(ports=(1935, 443)):
    """Established TCP connections from OBS-like ports: [(remote_ip, local_ip)]."""
    flt = " or ".join(f"dport = :{p}" for p in ports)
    rc, out, _ = run(["ss", "-tnH", "state", "established", f"( {flt} )"], timeout=3)
    conns = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4:
            local, remote = f[-2], f[-1]
            conns.append((remote.rsplit(":", 1)[0].strip("[]"), local.rsplit(":", 1)[0].strip("[]")))
    return conns
