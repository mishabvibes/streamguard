"""User settings in ~/.config/streamguard/config.json."""

import copy
import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)

# Ingest endpoints probed with a plain TCP connect (no ICMP privileges needed).
INGESTS = {
    "youtube": ("YouTube", "a.rtmp.youtube.com", 1935),
    "facebook": ("Facebook", "live-api-s.facebook.com", 443),
    "twitch": ("Twitch", "ingest.global-contribute.live-video.net", 1935),
    "instagram": ("Instagram", "live-upload.instagram.com", 443),
}

DEFAULTS = {
    "audio_device": None,          # display name of the console/interface to watch
    "video_devices": [],           # display names that must stay connected
    "silence_db": -50.0,           # below this peak level counts as silence
    "silence_seconds": 8,
    "clip_db": -0.5,
    "ingests": ["youtube", "facebook"],
    "ntfy_topic": "",              # phone push via https://ntfy.sh/<topic>
    "ntfy_server": "https://ntfy.sh",
    "alert_sound": False,          # off by default: the sound could leak into the stream
    "alert_sink": None,            # PipeWire/Pulse sink for alert sounds (e.g. headphones)
    "performance_when_live": True, # switch lpowerd to Performance while live
    "control_obs": True,           # GO LIVE / END STREAM also start/stop OBS
    "record_when_live": False,     # also start a local backup recording
    "follow_obs": True,            # starting the stream in OBS starts a session here
    "obs_port": None,              # None = read from OBS config
    "obs_password": None,
}


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "streamguard", "config.json")


class Config:
    def __init__(self, path=None):
        self.path = path or config_path()
        self.data = copy.deepcopy(DEFAULTS)
        try:
            with open(self.path) as f:
                saved = json.load(f)
            self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable config %s: %s", self.path, e)

    def __getitem__(self, k):
        return self.data[k]

    def set(self, **kw):
        self.data.update(kw)
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
