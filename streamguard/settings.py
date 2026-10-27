"""Settings dialog."""

import json
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from .config import INGESTS  # noqa: E402


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
        g = Adw.PreferencesGroup(title="OBS control")
        self.control_obs = Adw.SwitchRow(
            title="GO LIVE starts the stream in OBS",
            subtitle="END STREAM asks whether to stop it in OBS too",
            active=self.cfg["control_obs"])
        self.record = Adw.SwitchRow(title="Also record a local backup",
                                    subtitle="Started with the stream, stopped with it",
                                    active=self.cfg["record_when_live"])
        self.follow = Adw.SwitchRow(title="Follow OBS",
                                    subtitle="Starting the stream in OBS starts a session here",
                                    active=self.cfg["follow_obs"])
        for r in (self.control_obs, self.record, self.follow):
            g.add(r)
        page.add(g)
        g = Adw.PreferencesGroup(
            title="Multiple connections",
            description="Only while live, and only for the running session: nothing is saved "
                        "to your network settings and all of it is undone when you end.")
        self.failover = Adw.SwitchRow(
            title="Automatic failover",
            subtitle="If the main connection loses internet, switch to a working one",
            active=self.cfg["failover"])
        g.add(self.failover)
        links = self.engine.links.links
        self.link_devs = [None] + sorted(set(links) | {d for d in self.cfg["link_assign"].values() if d})
        names = ["Automatic (main connection)"] + [
            f"{links[d].label} ({d})" if d in links else f"{d} (not connected)"
            for d in self.link_devs[1:]]
        self.assign_rows = {}
        for key, (name, _h, _p) in INGESTS.items():
            r = Adw.ComboRow(title=f"Send {name} through", model=Gtk.StringList.new(names))
            cur = self.cfg["link_assign"].get(key)
            r.set_selected(self.link_devs.index(cur) if cur in self.link_devs else 0)
            self.assign_rows[key] = r
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
            control_obs=self.control_obs.get_active(),
            record_when_live=self.record.get_active(),
            follow_obs=self.follow.get_active(),
            failover=self.failover.get_active(),
            link_assign={k: self.link_devs[r.get_selected()] for k, r in self.assign_rows.items()
                         if r.get_selected() > 0},
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
